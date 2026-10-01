"""실제 PostgreSQL 에 붙는 Repository 테스트. TEST_DATABASE_URL 이 없으면 건너뛴다.

대상 DB 는 `alembic upgrade head` 가 끝난 상태여야 한다.
테스트는 트랜잭션을 롤백해 흔적을 남기지 않는다.
"""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.enums import DeploymentTrigger, Environment, ReleaseStatus
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.models.user import GithubInstallation, User
from app.repositories.project_repository import ProjectRepository, ServiceCounts
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(connection, expire_on_commit=False)
        async with factory() as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[User, Project, GithubInstallation]:
    user = User(github_id=900001, login="integration-user")
    installation = GithubInstallation(
        installation_id=900001, account_login="it", account_type="User"
    )
    session.add_all([user, installation])
    await session.flush()
    project = await ProjectRepository(session).save(Project(name="p", owner_id=user.id))
    return user, project, installation


async def test_seeded_targets_exist(session: AsyncSession) -> None:
    names = [t.name for t in await TargetRepository(session).search_all()]

    assert names == ["aws", "local"]


async def test_project_name_is_unique_per_owner_until_deleted(session: AsyncSession) -> None:
    user, project, _ = await _seed(session)
    repository = ProjectRepository(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await repository.save(Project(name="p", owner_id=user.id))

    project.mark_as_deleted()
    await session.flush()
    await repository.save(Project(name="p", owner_id=user.id))
    found = await repository.find_by_owner_id_and_name(user.id, "p")
    assert found is not None and found.id != project.id


async def test_count_services_counts_online_by_latest_release(session: AsyncSession) -> None:
    user, project, installation = await _seed(session)
    target_id = (await TargetRepository(session).search_all())[0].id
    services = ServiceRepository(session)
    online = await services.save(_service(project, installation, "online"))
    broken = await services.save(_service(project, installation, "broken"))
    await services.save(_service(project, installation, "never"))
    gone = await services.save(_service(project, installation, "gone"))
    gone.mark_as_deleted()
    for service, statuses in (
        (online, [ReleaseStatus.FAILED, ReleaseStatus.SUCCEEDED]),
        (broken, [ReleaseStatus.SUCCEEDED, ReleaseStatus.FAILED]),
    ):
        request = await _deployment_request(session, service, user)
        for release_status in statuses:
            session.add(
                Release(
                    deployment_request_id=request.id,
                    service_id=service.id,
                    environment=Environment.PROD,
                    target_id=target_id,
                    image_digest="sha256:" + "0" * 64,
                    status=release_status,
                )
            )
            await session.flush()

    counts = await ProjectRepository(session).count_services_by_project_ids([project.id])

    assert counts == {project.id: ServiceCounts(service_count=3, online_service_count=1)}


async def test_service_targets_are_replaced(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    first, second = [t.id for t in await TargetRepository(session).search_all()]
    services = ServiceRepository(session)
    service = await services.save(_service(project, installation, "web"))

    await services.replace_targets(service.id, {first, second})
    await services.replace_targets(service.id, {second})

    assert await services.search_target_ids_by_service_ids([service.id]) == {service.id: [second]}


async def test_find_service_by_id_requires_project_owner(session: AsyncSession) -> None:
    user, project, installation = await _seed(session)
    services = ServiceRepository(session)
    service = await services.save(_service(project, installation, "web"))

    assert await services.find_by_id_and_owner_id(service.id, user.id) is not None
    assert await services.find_by_id_and_owner_id(service.id, user.id + 1) is None


def _service(project: Project, installation: GithubInstallation, name: str) -> Service:
    return Service(
        project_id=project.id,
        name=name,
        source_repository_url="https://github.com/it/repo",
        github_installation_id=installation.id,
        source_branch="main",
    )


async def _deployment_request(
    session: AsyncSession, service: Service, user: User
) -> DeploymentRequest:
    request = DeploymentRequest(
        service_id=service.id,
        environment=Environment.PROD,
        source_sha="0" * 40,
        trigger_type=DeploymentTrigger.MANUAL,
        idempotency_key=f"it-{service.id}",
        requested_by=user.id,
    )
    session.add(request)
    await session.flush()
    return request
