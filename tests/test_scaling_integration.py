"""전용 PostgreSQL에서 PUT 저장·요청 스냅샷·Deploy Worker 흐름을 검증한다."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import DeploymentInProgressError
from app.enums import DeploymentStatus, DeploymentTrigger, JobKind
from app.models import Build, DeploymentRequest, Job, Project, Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import DeploymentStatusHistoryRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.scaling_config import ScalingConfig
from app.services.service_scaling_service import ServiceScalingDetail, ServiceScalingService
from tests.test_deploy_flow import Harness, _argo
from tests.worker_support import requires_database, session_factory_with_clean_data

pytestmark = [pytest.mark.integration, requires_database]

SCALED = ScalingConfig.model_validate(
    {
        "replicas": 3,
        "resources": {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
    }
)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


def _requests(session: AsyncSession) -> DeploymentRequestService:
    return DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ServiceVariableRepository(session),
        ServiceRepository(session),
    )


def _scaling(session: AsyncSession) -> ServiceScalingService:
    return ServiceScalingService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        _requests(session),
    )


async def _owner(session: AsyncSession, service_id: int) -> int:
    return (
        await session.execute(
            select(Project.owner_id)
            .join(Service, Service.project_id == Project.id)
            .where(Service.id == service_id)
        )
    ).scalar_one()


async def test_scaling_persists_and_worker_uses_snapshot_without_rebuilding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(session_factory)
    live_id = await h.deploy_successfully()
    assert h.service_id is not None
    async with session_factory() as session:
        owner_id = await _owner(session, h.service_id)
        detail = await _scaling(session).update_scaling(owner_id, h.service_id, SCALED)
    assert detail.deployment_request_id is not None
    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        request = await session.get_one(DeploymentRequest, detail.deployment_request_id)
        assert service.scaling_config == SCALED.model_dump(mode="json")
        assert request.scaling_snapshot == service.scaling_config
        assert request.source_deployment_request_id == live_id
        assert request.status == DeploymentStatus.DEPLOYING
        builds = list(await session.scalars(select(Build).order_by(Build.id)))
        assert len(builds) == 2
        assert builds[1].image_digest == builds[0].image_digest
        jobs = list(
            await session.scalars(select(Job).where(Job.deployment_request_id == request.id))
        )
        assert [job.kind for job in jobs] == [JobKind.DEPLOY]
        # Worker 처리 전에 원하는 설정이 바뀌어도 접수한 요청의 사양을 적용한다.
        newer = SCALED.model_copy(update={"replicas": 4})
        service.scaling_config = newer.model_dump(mode="json")

    await h.run_next(JobKind.DEPLOY)
    tree = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    values = json.loads(h.gitops.trees[tree]["values.yaml"])
    assert values["replicas"] == SCALED.replicas
    assert values["resources"] == SCALED.resources.model_dump(mode="json")
    assert values["image"]["digest"] == builds[0].image_digest
    h.argo.status = _argo(h.gitops.head)
    await h.run_next(JobKind.RECONCILE)
    request, _, _ = await h.load(detail.deployment_request_id)
    assert request.status == DeploymentStatus.SUCCEEDED

    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        next_request = await _requests(session).create_deployment_request(
            service,
            source_sha="b" * 40,
            source_commit_message=None,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key="next-after-scale",
        )
        assert next_request is not None
        assert next_request.scaling_snapshot == newer.model_dump(mode="json")


async def test_request_refreshes_scaling_after_slow_source_lookup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    assert h.service_id is not None
    async with session_factory() as stale_session:
        stale_service = await stale_session.get_one(Service, h.service_id)
        assert stale_service.scaling_config is None
        async with session_factory() as session:
            owner_id = await _owner(session, h.service_id)
            await _scaling(session).update_scaling(owner_id, h.service_id, SCALED)
        await h.run_next(JobKind.DEPLOY)
        h.argo.status = _argo(h.gitops.head)
        await h.run_next(JobKind.RECONCILE)
        request = await _requests(stale_session).create_deployment_request(
            stale_service,
            source_sha="b" * 40,
            source_commit_message=None,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key="slow-source-lookup",
        )
        assert stale_service.scaling_config is None
        assert request is not None
        assert request.scaling_snapshot == SCALED.model_dump(mode="json")
        await stale_session.commit()


async def test_simultaneous_scaling_accepts_one_config_and_one_deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    assert h.service_id is not None
    service_id = h.service_id
    async with session_factory() as session:
        owner_id = await _owner(session, service_id)

    async def submit(config: ScalingConfig) -> ServiceScalingDetail | DeploymentInProgressError:
        async with session_factory() as session:
            try:
                return await _scaling(session).update_scaling(owner_id, service_id, config)
            except DeploymentInProgressError as error:
                return error

    results = await asyncio.gather(
        submit(SCALED), submit(SCALED.model_copy(update={"replicas": 4}))
    )
    accepted = [result for result in results if isinstance(result, ServiceScalingDetail)]
    assert len(accepted) == 1
    assert sum(isinstance(result, DeploymentInProgressError) for result in results) == 1
    async with session_factory() as session:
        service = await session.get_one(Service, service_id)
        request = await session.get_one(DeploymentRequest, accepted[0].deployment_request_id)
        assert service.scaling_config == request.scaling_snapshot
        assert service.scaling_config == accepted[0].scaling.model_dump(mode="json")
        assert await session.scalar(select(func.count()).select_from(DeploymentRequest)) == 2


async def test_failed_job_insert_rolls_back_config_request_and_copied_build(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    assert h.service_id is not None

    async def fail_save(self: JobRepository, job: Job) -> Job:
        raise RuntimeError("job storage unavailable")

    monkeypatch.setattr(JobRepository, "save", fail_save)
    async with session_factory() as session:
        owner_id = await _owner(session, h.service_id)
        with pytest.raises(RuntimeError, match="job storage unavailable"):
            await _scaling(session).update_scaling(owner_id, h.service_id, SCALED)
    async with session_factory() as session:
        service = await session.get_one(Service, h.service_id)
        assert service.scaling_config is None
        assert await session.scalar(select(func.count()).select_from(DeploymentRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(Build)) == 1
