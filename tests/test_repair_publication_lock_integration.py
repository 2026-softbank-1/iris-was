from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ConflictError
from app.enums import DeploymentStatus, DeploymentTrigger, DiagnosisStatus, Environment
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_repair import DeploymentRepair
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


async def test_publication_lock_serializes_replicas_and_persists_resume_state(sessions):
    async with sessions() as seed:
        service = await seed_service(seed)
        owner = await seed.scalar(select(Project.owner_id).where(Project.id == service.project_id))
        deployment = await add(
            seed,
            DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha="a" * 40,
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key="lock-test",
                requested_by=owner,
                status=DeploymentStatus.FAILED,
            ),
        )
        diagnosis = await add(
            seed,
            DeploymentDiagnosis(
                deployment_request_id=deployment.id, status=DiagnosisStatus.SUCCEEDED, result={}
            ),
        )
        repair = await add(
            seed,
            DeploymentRepair(
                service_id=service.id,
                deployment_request_id=deployment.id,
                diagnosis_id=diagnosis.id,
                requested_by=owner,
                idempotency_key="lock-test",
                input_digest="d" * 64,
                source_sha="a" * 40,
                source_repository_url=service.source_repository_url,
                root_directory=".",
                plan_ids=["R1"],
                diagnosis_result={},
                request_metadata={},
                status="SUCCEEDED",
                deadline_at=now_utc() + timedelta(minutes=5),
            ),
        )
        repair_id = repair.id
        await seed.commit()
    async with sessions() as first, sessions() as second:
        locked = await DeploymentRepairRepository(first).lock_publication(repair_id)
        with pytest.raises(ConflictError, match="already running"):
            await DeploymentRepairRepository(second).lock_publication(repair_id)
        locked.request_metadata = {
            "publication": {"status": "PR_OPENED", "pullUrl": "https://github.com/o/r/pull/1"}
        }
        await first.commit()
        resumed = await DeploymentRepairRepository(second).lock_publication(repair_id)
        assert resumed.request_metadata["publication"]["status"] == "PR_OPENED"
        assert resumed.request_metadata["publication"]["pullUrl"] == "https://github.com/o/r/pull/1"
        await second.rollback()
