import pytest

from app.core.exceptions import InvalidInputError, ProjectNameConflictError, ProjectNotFoundError
from app.models.service import Service
from app.repositories.project_repository import ServiceCounts
from app.services.project_service import ProjectService
from tests.fakes import FakeSession
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository


@pytest.fixture
def parts() -> tuple[ProjectService, FakeProjectRepository, FakeServiceRepository, FakeSession]:
    session = FakeSession()
    projects = FakeProjectRepository()
    services = FakeServiceRepository(projects)
    service = ProjectService(session, projects, services)  # type: ignore[arg-type]
    return service, projects, services, session


async def test_create_project_commits_and_starts_with_no_services(parts) -> None:
    service, _, _, session = parts

    summary = await service.create_project(1, "shop", "demo")

    assert (summary.project.name, summary.project.owner_id) == ("shop", 1)
    assert summary.counts == ServiceCounts(0, 0)
    assert session.commit_count == 1


async def test_create_project_with_duplicate_name_raises_conflict(parts) -> None:
    service, *_ = parts
    await service.create_project(1, "shop", None)

    with pytest.raises(ProjectNameConflictError):
        await service.create_project(1, "shop", None)


async def test_same_project_name_is_allowed_for_another_owner(parts) -> None:
    service, *_ = parts
    await service.create_project(1, "shop", None)

    summary = await service.create_project(2, "shop", None)

    assert summary.project.owner_id == 2


async def test_get_project_of_another_owner_raises_not_found(parts) -> None:
    service, *_ = parts
    created = await service.create_project(1, "shop", None)

    with pytest.raises(ProjectNotFoundError):
        await service.get_project(2, created.project.id)


async def test_search_projects_returns_newest_first_with_counts(parts) -> None:
    service, projects, *_ = parts
    first = await service.create_project(1, "a", None)
    second = await service.create_project(1, "b", None)
    await service.create_project(2, "other", None)
    projects.counts[first.project.id] = ServiceCounts(3, 2)

    summaries = await service.search_projects(1)

    assert [s.project.id for s in summaries] == [second.project.id, first.project.id]
    assert summaries[1].counts == ServiceCounts(3, 2)
    assert summaries[0].counts == ServiceCounts(0, 0)


async def test_update_project_changes_only_given_fields(parts) -> None:
    service, *_ = parts
    created = await service.create_project(1, "shop", "old")

    summary = await service.update_project(1, created.project.id, {"description": None})

    assert summary.project.name == "shop"
    assert summary.project.description is None


async def test_update_project_rename_to_existing_name_raises_conflict(parts) -> None:
    service, *_ = parts
    await service.create_project(1, "a", None)
    other = await service.create_project(1, "b", None)

    with pytest.raises(ProjectNameConflictError):
        await service.update_project(1, other.project.id, {"name": "a"})


async def test_update_project_keeping_same_name_is_allowed(parts) -> None:
    service, *_ = parts
    created = await service.create_project(1, "a", None)

    summary = await service.update_project(1, created.project.id, {"name": "a"})

    assert summary.project.name == "a"


async def test_update_project_with_null_name_raises_invalid_input(parts) -> None:
    service, *_ = parts
    created = await service.create_project(1, "a", None)

    with pytest.raises(InvalidInputError):
        await service.update_project(1, created.project.id, {"name": None})


async def test_delete_project_soft_deletes_project_and_services(parts) -> None:
    service, projects, services, session = parts
    created = await service.create_project(1, "shop", None)
    child = await services.save(Service(project_id=created.project.id, name="web"))

    await service.delete_project(1, created.project.id)

    assert projects.projects[created.project.id].is_deleted is True
    assert child.is_deleted is True
    assert session.commit_count == 2
    with pytest.raises(ProjectNotFoundError):
        await service.get_project(1, created.project.id)
    # 삭제한 이름은 다시 쓸 수 있다.
    assert (await service.create_project(1, "shop", None)).project.name == "shop"
