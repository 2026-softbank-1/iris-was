import pytest

from app.clients.source_repository_client import BranchInfo
from app.core.exceptions import (
    InvalidInputError,
    ProjectNotFoundError,
    RepositoryNotAccessibleError,
    ServiceNameConflictError,
    ServiceNotFoundError,
)
from app.enums import Builder
from app.models.project import Project
from app.services.service_registry_service import (
    ServiceRegistryService,
    normalize_root_directory,
    slugify_service_name,
)
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSession,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository, FakeTargetRepository

OWNER = 1


class Setup:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.targets = FakeTargetRepository()
        self.installations = FakeGithubInstallationRepository()
        self.github = FakeSourceRepositoryClient({22: [make_repository("iris-org/My_Web.App")]})
        self.github.branches["iris-org/My_Web.App"] = [
            BranchInfo("main", True),
            BranchInfo("dev", False),
        ]
        self.project_id = 0

    async def build(self) -> ServiceRegistryService:
        installation = await self.installations.save(make_installation(5, 22, "iris-org"))
        await self.installations.replace_user_links(OWNER, {installation.id})
        project = await self.projects.save(Project(name="p", owner_id=OWNER))
        self.project_id = project.id
        return ServiceRegistryService(
            self.session,  # type: ignore[arg-type]
            self.projects,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.installations,  # type: ignore[arg-type]
            SourceRepositoryService(self.installations, self.github),  # type: ignore[arg-type]
        )


@pytest.fixture
async def setup() -> tuple[Setup, ServiceRegistryService]:
    s = Setup()
    return s, await s.build()


async def _create(service: ServiceRegistryService, s: Setup, **overrides):
    args = dict(
        repository_url="https://github.com/iris-org/My_Web.App",
        name=None,
        branch=None,
        root_directory=None,
        is_auto_deploy=True,
        target_ids=None,
    )
    args.update(overrides)
    return await service.create_service(OWNER, s.project_id, **args)


def test_slugify_service_name_makes_dns_label() -> None:
    assert slugify_service_name("My_Web.App") == "my-web-app"
    assert slugify_service_name("___") == "service"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("/", None),
        (".", None),
        ("apps//web/", "apps/web"),
        ("./a/./b", "a/b"),
    ],
)
def test_normalize_root_directory(value: str | None, expected: str | None) -> None:
    assert normalize_root_directory(value) == expected


def test_normalize_root_directory_rejects_parent_traversal() -> None:
    with pytest.raises(InvalidInputError):
        normalize_root_directory("../secrets")


async def test_create_service_uses_repo_defaults_and_all_targets(setup) -> None:
    s, service = setup

    detail = await _create(service, s)

    assert detail.service.name == "my-web-app"
    assert detail.service.source_branch == "main"
    assert detail.service.source_repository_url == "https://github.com/iris-org/My_Web.App"
    assert detail.service.github_installation_id == 5
    assert detail.target_ids == [1, 2]
    assert s.session.commit_count == 1


async def test_create_service_with_explicit_values(setup) -> None:
    s, service = setup

    detail = await _create(
        service,
        s,
        name="web",
        branch="dev",
        root_directory="/apps/web/",
        is_auto_deploy=False,
        target_ids=[2],
    )

    assert (detail.service.name, detail.service.source_branch) == ("web", "dev")
    assert detail.service.root_directory == "apps/web"
    assert detail.service.is_auto_deploy is False
    assert detail.target_ids == [2]


async def test_create_service_with_unknown_branch_raises_invalid_input(setup) -> None:
    s, service = setup

    with pytest.raises(InvalidInputError):
        await _create(service, s, branch="ghost")


async def test_create_service_with_unknown_target_raises_invalid_input(setup) -> None:
    s, service = setup

    with pytest.raises(InvalidInputError):
        await _create(service, s, target_ids=[1, 99])


async def test_create_service_with_empty_targets_raises_invalid_input(setup) -> None:
    s, service = setup

    with pytest.raises(InvalidInputError):
        await _create(service, s, target_ids=[])


async def test_create_service_with_uppercase_name_raises_invalid_input(setup) -> None:
    s, service = setup

    with pytest.raises(InvalidInputError):
        await _create(service, s, name="My App")


async def test_create_service_with_duplicate_name_raises_conflict(setup) -> None:
    s, service = setup
    await _create(service, s)

    with pytest.raises(ServiceNameConflictError):
        await _create(service, s)


async def test_create_service_in_other_owners_project_raises_not_found(setup) -> None:
    s, service = setup

    with pytest.raises(ProjectNotFoundError):
        await service.create_service(2, s.project_id, "iris-org/x", None, None, None, True, None)


async def test_create_service_for_inaccessible_repository_raises_forbidden(setup) -> None:
    s, service = setup

    with pytest.raises(RepositoryNotAccessibleError):
        await _create(service, s, repository_url="https://github.com/stranger/repo")


async def test_get_service_of_other_owner_raises_not_found(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    with pytest.raises(ServiceNotFoundError):
        await service.get_service(2, detail.service.id)


async def test_search_services_returns_targets(setup) -> None:
    s, service = setup
    await _create(service, s, name="a")
    await _create(service, s, name="b", target_ids=[1])

    details = await service.search_services(OWNER, s.project_id)

    assert [(d.service.name, d.target_ids) for d in details] == [("a", [1, 2]), ("b", [1])]


async def test_update_service_changes_build_settings_and_clears_nulls(setup) -> None:
    s, service = setup
    detail = await _create(service, s, root_directory="apps/web")

    updated = await service.update_service(
        OWNER,
        detail.service.id,
        {
            "builder": "dockerfile",
            "dockerfile_path": "Dockerfile",
            "port": 8080,
            "root_directory": None,
        },
    )

    assert updated.service.builder is Builder.DOCKERFILE
    assert (updated.service.dockerfile_path, updated.service.port) == ("Dockerfile", 8080)
    assert updated.service.root_directory is None
    assert updated.target_ids == [1, 2]


async def test_update_service_replaces_targets(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    updated = await service.update_service(OWNER, detail.service.id, {"target_ids": [2]})

    assert updated.target_ids == [2]
    assert s.services.targets[detail.service.id] == {2}


@pytest.mark.parametrize("field", ["name", "source_branch", "is_auto_deploy", "target_ids"])
async def test_update_service_rejects_null_for_required_fields(setup, field: str) -> None:
    s, service = setup
    detail = await _create(service, s)

    with pytest.raises(InvalidInputError):
        await service.update_service(OWNER, detail.service.id, {field: None})


async def test_update_service_rename_to_existing_name_raises_conflict(setup) -> None:
    s, service = setup
    await _create(service, s, name="a")
    other = await _create(service, s, name="b")

    with pytest.raises(ServiceNameConflictError):
        await service.update_service(OWNER, other.service.id, {"name": "a"})


async def test_update_service_to_unknown_branch_raises_invalid_input(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    with pytest.raises(InvalidInputError):
        await service.update_service(OWNER, detail.service.id, {"source_branch": "ghost"})


async def test_update_service_to_existing_branch_succeeds(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    updated = await service.update_service(OWNER, detail.service.id, {"source_branch": "dev"})

    assert updated.service.source_branch == "dev"


async def test_delete_service_soft_deletes_and_frees_name(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    await service.delete_service(OWNER, detail.service.id)

    assert detail.service.is_deleted is True
    with pytest.raises(ServiceNotFoundError):
        await service.get_service(OWNER, detail.service.id)
    await _create(service, s)
