"""실제 PostgreSQL 에 붙는 Repository 테스트. TEST_DATABASE_URL 이 없으면 건너뛴다.

대상 DB 는 `alembic upgrade head` 가 끝난 상태여야 한다.
테스트는 트랜잭션을 롤백해 흔적을 남기지 않는다.
"""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.exceptions import InvalidStatusTransitionError
from app.enums import (
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
    ReleaseStatus,
)
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.models.user import GithubInstallation, User
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.repositories.project_repository import ProjectRepository, ServiceCounts
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.repositories.target_repository import TargetRepository
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService

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
        build = Build(deployment_request_id=request.id)
        session.add(build)
        await session.flush()
        for release_status in statuses:
            session.add(
                Release(
                    deployment_request_id=request.id,
                    build_id=build.id,
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


async def test_add_if_absent_blocks_duplicate_key_and_active_request(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    repository = DeploymentRequestRepository(session)

    def build(key: str) -> DeploymentRequest:
        return DeploymentRequest(
            service_id=service.id,
            environment=Environment.PROD,
            source_sha="a" * 40,
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=key,
        )

    first = await repository.add_if_absent(build("k-1"))
    assert first is not None and first.status == DeploymentStatus.QUEUED
    assert await repository.add_if_absent(build("k-1")) is None  # 같은 키
    assert await repository.add_if_absent(build("k-2")) is None  # 진행 중인 요청이 있다

    first.status = DeploymentStatus.SUCCEEDED
    await session.flush()
    assert await repository.add_if_absent(build("k-2")) is not None
    assert await repository.find_by_idempotency_key("k-2") is not None


async def test_search_auto_deploy_by_repository_url_ignores_case_and_filters(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    services = ServiceRepository(session)
    web = await services.save(_service(project, installation, "web"))
    off = _service(project, installation, "off")
    off.is_auto_deploy = False
    await services.save(off)
    other_branch = _service(project, installation, "dev")
    other_branch.source_branch = "develop"
    await services.save(other_branch)

    found = await services.search_auto_deploy_by_repository_url_and_branch(
        "https://github.com/IT/Repo", "main"
    )

    assert [s.id for s in found] == [web.id]


async def test_search_latest_by_service_ids_returns_newest_request_per_service(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    services = ServiceRepository(session)
    web = await services.save(_service(project, installation, "web"))
    api = await services.save(_service(project, installation, "api"))
    idle = await services.save(_service(project, installation, "idle"))
    repository = DeploymentRequestRepository(session)

    async def add(service: Service, key: str, status: DeploymentStatus) -> DeploymentRequest:
        request = DeploymentRequest(
            service_id=service.id,
            environment=Environment.PROD,
            source_sha=key.ljust(40, "0"),
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=f"latest-{service.id}-{key}",
            status=status,
        )
        session.add(request)
        await session.flush()
        return request

    await add(web, "a", DeploymentStatus.FAILED)
    newest_web = await add(web, "b", DeploymentStatus.SUCCEEDED)
    only_api = await add(api, "c", DeploymentStatus.BUILDING)

    latest = await repository.search_latest_by_service_ids([web.id, api.id, idle.id])

    assert {k: v.id for k, v in latest.items()} == {web.id: newest_web.id, api.id: only_api.id}


async def _queued_request(session: AsyncSession, service: Service) -> DeploymentRequest:
    request = await DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ServiceVariableRepository(session),
        ServiceRepository(session),
    ).create_deployment_request(
        service,
        source_sha="c" * 40,
        source_commit_message="feat: x",
        trigger_type=DeploymentTrigger.MANUAL,
        idempotency_key=f"it-flow-{service.id}",
    )
    assert request is not None
    return request


async def test_transition_status_full_flow_and_rejected_step(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    request = await _queued_request(session, service)
    history = DeploymentStatusHistoryRepository(session)
    status_service = DeploymentStatusService(DeploymentRequestRepository(session), history)

    await status_service.transition_status(request.id, DeploymentStatus.BUILDING)
    with pytest.raises(InvalidStatusTransitionError):
        await status_service.transition_status(request.id, DeploymentStatus.SUCCEEDED)
    await status_service.transition_status(request.id, DeploymentStatus.DEPLOYING)
    await status_service.transition_status(
        request.id, DeploymentStatus.FAILED, failure_code=FailureCode.DEPLOY_FAILED
    )
    await status_service.transition_status(request.id, DeploymentStatus.ROLLED_BACK)

    rows = await history.search_by_deployment_request_id(request.id)
    assert [(r.from_status, r.to_status) for r in rows] == [
        (None, DeploymentStatus.QUEUED),
        (DeploymentStatus.QUEUED, DeploymentStatus.BUILDING),
        (DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING),
        (DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED),
        (DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK),
    ]
    assert rows[3].failure_code == FailureCode.DEPLOY_FAILED
    stored = await DeploymentRequestRepository(session).get_by_id_for_update(request.id)
    assert (stored.status, stored.failure_code) == (
        DeploymentStatus.ROLLED_BACK,
        FailureCode.DEPLOY_FAILED,
    )


async def test_get_by_id_for_update_refreshes_status_changed_elsewhere(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    request = await _queued_request(session, service)
    await session.execute(
        update(DeploymentRequest)
        .where(DeploymentRequest.id == request.id)
        .values(status=DeploymentStatus.BUILDING)
    )

    locked = await DeploymentRequestRepository(session).get_by_id_for_update(request.id)

    assert locked.status == DeploymentStatus.BUILDING


async def test_search_by_service_id_orders_newest_first_and_counts(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    services = ServiceRepository(session)
    web = await services.save(_service(project, installation, "web"))
    api = await services.save(_service(project, installation, "api"))
    repository = DeploymentRequestRepository(session)
    created_ids: list[int] = []
    for index in range(3):
        request = DeploymentRequest(
            service_id=web.id,
            environment=Environment.PROD,
            source_sha=str(index).ljust(40, "0"),
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=f"search-{web.id}-{index}",
            status=DeploymentStatus.SUCCEEDED,
        )
        session.add(request)
        await session.flush()
        created_ids.append(request.id)
    other = DeploymentRequest(
        service_id=api.id,
        environment=Environment.PROD,
        source_sha="e" * 40,
        trigger_type=DeploymentTrigger.PUSH,
        idempotency_key=f"search-{api.id}",
    )
    session.add(other)
    await session.flush()

    first_page = await repository.search_by_service_id(web.id, page=0, size=2)
    second_page = await repository.search_by_service_id(web.id, page=1, size=2)

    assert [r.id for r in first_page] == [created_ids[2], created_ids[1]]
    assert [r.id for r in second_page] == [created_ids[0]]
    assert await repository.count_by_service_id(web.id) == 3
    assert await repository.find_by_id_and_service_id(other.id, web.id) is None


async def test_deployment_status_history_rejects_unknown_status(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    request = await _queued_request(session, service)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO deployment_status_histories (deployment_request_id, to_status) "
                    "VALUES (:id, 'CRASHED')"
                ),
                {"id": request.id},
            )


async def test_service_variable_add_if_absent_skips_existing_key(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    repository = ServiceVariableRepository(session)

    first = await repository.add_if_absent(service.id, "A", "enc-1")
    duplicate = await repository.add_if_absent(service.id, "A", "enc-2")

    assert first is not None and first.id is not None
    assert duplicate is None
    found = await repository.find_by_service_id_and_key(service.id, "A")
    assert found is not None and found.encrypted_value == "enc-1"


async def test_service_variable_replace_all_upserts_and_removes_missing_keys(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    services = ServiceRepository(session)
    web = await services.save(_service(project, installation, "web"))
    api = await services.save(_service(project, installation, "api"))
    repository = ServiceVariableRepository(session)
    await repository.replace_all(web.id, {"KEEP": "old", "DROP": "x"})
    await repository.replace_all(api.id, {"OTHER": "y"})

    await repository.replace_all(web.id, {"KEEP": "new", "ADDED": "z"})

    found = await repository.search_by_service_id(web.id)
    assert [(v.key, v.encrypted_value) for v in found] == [("ADDED", "z"), ("KEEP", "new")]
    assert [v.key for v in await repository.search_by_service_id(api.id)] == ["OTHER"]


async def test_service_variable_replace_all_empty_clears_only_that_service(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    services = ServiceRepository(session)
    web = await services.save(_service(project, installation, "web"))
    api = await services.save(_service(project, installation, "api"))
    repository = ServiceVariableRepository(session)
    await repository.replace_all(web.id, {"A": "1"})
    await repository.replace_all(api.id, {"B": "2"})

    await repository.replace_all(web.id, {})

    assert await repository.search_by_service_id(web.id) == []
    assert [v.key for v in await repository.search_by_service_id(api.id)] == ["B"]


async def test_service_variable_delete_removes_row(session: AsyncSession) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    repository = ServiceVariableRepository(session)
    variable = await repository.add_if_absent(service.id, "A", "1")
    assert variable is not None

    await repository.delete(variable)

    assert await repository.find_by_service_id_and_key(service.id, "A") is None


async def test_deployment_request_service_snapshots_variables_into_jsonb(
    session: AsyncSession,
) -> None:
    _, project, installation = await _seed(session)
    service = await ServiceRepository(session).save(_service(project, installation, "web"))
    await ServiceVariableRepository(session).replace_all(service.id, {"A": "enc-a", "B": "enc-b"})

    request = await _queued_request(session, service)

    stored = await DeploymentRequestRepository(session).find_by_id_and_service_id(
        request.id, service.id
    )
    assert stored is not None
    assert stored.variables_snapshot == {"A": "enc-a", "B": "enc-b"}
