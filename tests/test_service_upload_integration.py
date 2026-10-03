"""업로드 묶기의 원자성을 실제 PostgreSQL 로 검증한다. TEST_DATABASE_URL 이 필요하다.

핵심: 동시에 들어온 요청 중 하나만 업로드를 가져가고, 요청을 만들지 못하면 업로드는 되돌아간다.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import (
    DeploymentInProgressError,
    UploadNotFoundError,
    UploadUnavailableError,
)
from app.enums import DeploymentStatus, DeploymentTrigger, Environment
from app.models import DeploymentRequest, Project, Service, ServiceUpload
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import DeploymentStatusHistoryRepository
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_upload_repository import ServiceUploadRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import FakeSourceRepositoryClient
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


def _manual_service(session: AsyncSession) -> ManualDeploymentService:
    return ManualDeploymentService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        DeploymentRequestService(
            DeploymentRequestRepository(session),
            JobRepository(session),
            DeploymentStatusHistoryRepository(session),
            BuildRepository(session),
            ServiceVariableRepository(session),
            ServiceRepository(session),
        ),
        SourceRepositoryService(
            GithubInstallationRepository(session),
            FakeSourceRepositoryClient({}),  # type: ignore[arg-type]
        ),
        ServiceUploadRepository(session),
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    public_id: str = "up-1",
    *,
    expires_in: timedelta = timedelta(hours=1),
) -> tuple[int, int, int]:
    """(owner_id, service_id, upload_id)"""
    async with session_factory.begin() as session:
        service = await seed_service(session)
        owner_id = (await session.get_one(Project, service.project_id)).owner_id
        upload = await add(
            session,
            ServiceUpload(
                public_id=public_id,
                service_id=service.id,
                uploaded_by=owner_id,
                size_bytes=10,
                sha256="a" * 64,
                storage_key=f"uploads/{public_id}.tar.gz",
                expires_at=datetime.now(UTC) + expires_in,
            ),
        )
        return owner_id, service.id, upload.id


async def _upload(session_factory: async_sessionmaker[AsyncSession], upload_id: int) -> Any:
    async with session_factory() as session:
        return await session.get_one(ServiceUpload, upload_id)


async def _create_cli(
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: int,
    service_id: int,
    upload_id: str = "up-1",
    key: str | None = None,
) -> DeploymentRequest:
    async with session_factory() as session:
        return await _manual_service(session).create_deployment_request(
            owner_id,
            service_id,
            trigger_type=DeploymentTrigger.CLI,
            upload_id=upload_id,
            idempotency_key=key,
        )


async def test_claim_takes_an_upload_only_once(session_factory: Any) -> None:
    _, _, upload_id = await _seed(session_factory)
    now = datetime.now(UTC)

    async with session_factory.begin() as session:
        first = await ServiceUploadRepository(session).claim(upload_id, now)
    async with session_factory.begin() as session:
        second = await ServiceUploadRepository(session).claim(upload_id, now)

    assert (first, second) == (True, False)
    assert (await _upload(session_factory, upload_id)).consumed_at is not None


async def test_claim_refuses_expired_upload(session_factory: Any) -> None:
    _, _, upload_id = await _seed(session_factory, expires_in=timedelta(seconds=-1))

    async with session_factory.begin() as session:
        claimed = await ServiceUploadRepository(session).claim(upload_id, datetime.now(UTC))

    assert claimed is False


async def test_claim_is_undone_when_the_transaction_rolls_back(session_factory: Any) -> None:
    _, _, upload_id = await _seed(session_factory)
    now = datetime.now(UTC)

    async with session_factory() as session:
        assert await ServiceUploadRepository(session).claim(upload_id, now) is True
        await session.rollback()
    async with session_factory.begin() as session:
        again = await ServiceUploadRepository(session).claim(upload_id, now)

    assert again is True


async def test_concurrent_claims_have_a_single_winner(session_factory: Any) -> None:
    _, _, upload_id = await _seed(session_factory)
    now = datetime.now(UTC)

    async def claim() -> bool:
        async with session_factory.begin() as session:
            claimed = await ServiceUploadRepository(session).claim(upload_id, now)
            await asyncio.sleep(0.05)  # 이긴 쪽이 커밋하기 전까지 진 쪽이 기다리게 한다.
            return claimed

    results = await asyncio.gather(*(claim() for _ in range(8)))

    assert sorted(results) == [False] * 7 + [True]


async def test_cli_request_binds_upload_and_persists_the_link(session_factory: Any) -> None:
    owner_id, service_id, upload_id = await _seed(session_factory)

    request = await _create_cli(session_factory, owner_id, service_id)

    stored = await _upload(session_factory, upload_id)
    async with session_factory() as session:
        row = (await session.scalars(select(DeploymentRequest))).one()
    assert stored.consumed_at is not None
    assert (row.id, row.service_upload_id, row.trigger_type) == (
        request.id,
        upload_id,
        DeploymentTrigger.CLI,
    )
    assert row.source_sha == "upload-" + "a" * 12


async def test_concurrent_cli_requests_with_one_upload_create_exactly_one_request(
    session_factory: Any,
) -> None:
    owner_id, service_id, _ = await _seed(session_factory)

    results = await asyncio.gather(
        *(_create_cli(session_factory, owner_id, service_id, key=f"k{i}") for i in range(6)),
        return_exceptions=True,
    )

    created = [r for r in results if isinstance(r, DeploymentRequest)]
    rejected = [r for r in results if isinstance(r, UploadUnavailableError)]
    assert (len(created), len(rejected)) == (1, 5)
    async with session_factory() as session:
        assert len((await session.scalars(select(DeploymentRequest))).all()) == 1


async def test_cli_request_blocked_by_active_deployment_gives_the_upload_back(
    session_factory: Any,
) -> None:
    owner_id, service_id, upload_id = await _seed(session_factory)
    async with session_factory.begin() as session:
        await add(
            session,
            DeploymentRequest(
                service_id=service_id,
                environment=Environment.PROD,
                source_sha="a" * 40,
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key=uuid4().hex,
                status=DeploymentStatus.BUILDING,
            ),
        )

    with pytest.raises(DeploymentInProgressError):
        await _create_cli(session_factory, owner_id, service_id)

    assert (await _upload(session_factory, upload_id)).consumed_at is None
    async with session_factory.begin() as session:
        await session.execute(
            DeploymentRequest.__table__.update().values(status=DeploymentStatus.SUCCEEDED)
        )
    request = await _create_cli(session_factory, owner_id, service_id)
    assert request.service_upload_id == upload_id


async def test_cli_request_retry_with_same_key_replays_after_commit(session_factory: Any) -> None:
    owner_id, service_id, _ = await _seed(session_factory)

    first = await _create_cli(session_factory, owner_id, service_id, key="up-up-1")
    second = await _create_cli(session_factory, owner_id, service_id, key="up-up-1")

    assert second.id == first.id


async def test_cli_request_with_other_services_upload_is_not_found(session_factory: Any) -> None:
    owner_id, service_id, _ = await _seed(session_factory)
    async with session_factory.begin() as session:
        other = await add(
            session,
            Service(
                project_id=(await session.get_one(Service, service_id)).project_id,
                name="api",
                source_repository_url="https://github.com/owner/other",
                github_installation_id=(
                    await session.get_one(Service, service_id)
                ).github_installation_id,
                source_branch="main",
            ),
        )
        other_id = other.id

    with pytest.raises(UploadNotFoundError):
        await _create_cli(session_factory, owner_id, other_id)


async def test_database_rejects_one_upload_on_two_requests(session_factory: Any) -> None:
    _, service_id, upload_id = await _seed(session_factory)

    def request(status: DeploymentStatus) -> DeploymentRequest:
        return DeploymentRequest(
            service_id=service_id,
            environment=Environment.PROD,
            source_sha="upload-" + "a" * 12,
            trigger_type=DeploymentTrigger.CLI,
            idempotency_key=uuid4().hex,
            status=status,
            service_upload_id=upload_id,
        )

    async with session_factory.begin() as session:
        await add(session, request(DeploymentStatus.SUCCEEDED))

    with pytest.raises(IntegrityError, match="uq_deployment_requests_service_upload_id"):
        async with session_factory.begin() as session:
            await add(session, request(DeploymentStatus.SUCCEEDED))


async def test_delete_unused_expired_keeps_used_and_recent_uploads(session_factory: Any) -> None:
    owner_id, service_id, _ = await _seed(session_factory, public_id="fresh")
    now = datetime.now(UTC)

    def stale(public_id: str, expires_in: timedelta, consumed: bool) -> ServiceUpload:
        return ServiceUpload(
            public_id=public_id,
            service_id=service_id,
            uploaded_by=owner_id,
            size_bytes=1,
            sha256="b" * 64,
            storage_key=f"uploads/{public_id}.tar.gz",
            expires_at=now + expires_in,
            consumed_at=now if consumed else None,
        )

    async with session_factory.begin() as session:
        await add(session, stale("old-unused", timedelta(days=-3), False))
        await add(session, stale("old-used", timedelta(days=-3), True))
        await add(session, stale("recent-unused", timedelta(hours=-2), False))
    async with session_factory.begin() as session:
        await ServiceUploadRepository(session).delete_unused_expired_before(now - timedelta(days=1))

    async with session_factory() as session:
        remaining = sorted((await session.scalars(select(ServiceUpload.public_id))).all())
    assert remaining == ["fresh", "old-used", "recent-unused"]


async def test_public_id_is_unique(session_factory: Any) -> None:
    owner_id, service_id, _ = await _seed(session_factory)

    with pytest.raises(IntegrityError, match="uq_service_uploads_public_id"):
        async with session_factory.begin() as session:
            await add(
                session,
                ServiceUpload(
                    public_id="up-1",
                    service_id=service_id,
                    uploaded_by=owner_id,
                    size_bytes=1,
                    sha256="c" * 64,
                    storage_key="uploads/dup.tar.gz",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
