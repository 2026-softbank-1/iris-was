from types import SimpleNamespace

import pytest

from app.clients.source_repository_client import BranchInfo
from app.core.exceptions import (
    ConflictError,
    DeploymentInProgressError,
    InvalidInputError,
    ProjectNotFoundError,
    RepositoryNotAccessibleError,
    ServiceNameConflictError,
    ServiceNotFoundError,
)
from app.enums import Builder, DeploymentStrategy, DeploymentTrigger, Environment
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.services.scaling_config import ScalingConfig
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
from tests.fakes_project import (
    FakeProjectRepository,
    FakeServiceRepository,
    FakeTargetRepository,
    FakeTeardownService,
)
from tests.fakes_webhook import FakeDeploymentRequestRepository

OWNER = 1


class Setup:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.targets = FakeTargetRepository()
        self.installations = FakeGithubInstallationRepository()
        self.deployments = FakeDeploymentRequestRepository()
        self.teardown = FakeTeardownService()
        self.github = FakeSourceRepositoryClient({22: [make_repository("iris-org/My_Web.App")]})
        self.github.branches["iris-org/My_Web.App"] = [
            BranchInfo("main", True),
            BranchInfo("dev", False),
        ]
        self.project_id = 0

    async def build(self, *, deployment_strategy_enabled: bool = True) -> ServiceRegistryService:
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
            self.deployments,  # type: ignore[arg-type]
            self.teardown,  # type: ignore[arg-type]
            deployment_strategy_enabled=deployment_strategy_enabled,
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


async def test_create_service_uses_repo_defaults_and_aws_target(setup) -> None:
    s, service = setup

    detail = await _create(service, s)

    assert detail.service.name == "my-web-app"
    assert detail.service.source_branch == "main"
    assert detail.service.source_repository_url == "https://github.com/iris-org/My_Web.App"
    assert detail.service.github_installation_id == 5
    assert detail.target_ids == [1]
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

    assert [(d.service.name, d.target_ids) for d in details] == [("a", [1]), ("b", [1])]


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
    assert updated.target_ids == [1]


async def test_update_service_replaces_targets(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    updated = await service.update_service(OWNER, detail.service.id, {"target_ids": [2]})

    assert updated.target_ids == [2]
    assert s.services.targets[detail.service.id] == {2}


async def test_create_service_with_two_targets_raises_invalid_input(setup) -> None:
    s, service = setup
    with pytest.raises(InvalidInputError):
        await _create(service, s, target_ids=[1, 2])


async def test_update_service_rejects_target_change_after_deployment(setup) -> None:
    s, service = setup
    detail = await _create(service, s)
    s.deployments.requests.append(SimpleNamespace(id=1, service_id=detail.service.id))  # type: ignore[arg-type]
    with pytest.raises(ConflictError):
        await service.update_service(OWNER, detail.service.id, {"target_ids": [2]})


async def test_update_service_keeps_same_target_after_deployment(setup) -> None:
    s, service = setup
    detail = await _create(service, s)
    s.deployments.requests.append(SimpleNamespace(id=1, service_id=detail.service.id))  # type: ignore[arg-type]
    updated = await service.update_service(OWNER, detail.service.id, {"target_ids": [1]})
    assert updated.target_ids == [1]


@pytest.mark.parametrize(
    "field", ["name", "source_branch", "is_auto_deploy", "target_ids", "deployment_strategy"]
)
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


async def test_delete_service_requests_teardown_of_running_app(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    await service.delete_service(OWNER, detail.service.id)

    assert s.teardown.calls == [([detail.service.id], OWNER)]


async def test_delete_service_with_deployment_in_progress_keeps_service(setup) -> None:
    s, service = setup
    detail = await _create(service, s)
    s.teardown.error = DeploymentInProgressError(
        "a deployment is in progress", service_id=detail.service.id
    )
    commits_before = s.session.commit_count

    with pytest.raises(DeploymentInProgressError):
        await service.delete_service(OWNER, detail.service.id)

    assert detail.service.is_deleted is False
    assert s.session.commit_count == commits_before


async def test_get_service_without_deployment_has_no_latest_deployment(setup) -> None:
    s, service = setup
    detail = await _create(service, s)

    found = await service.get_service(OWNER, detail.service.id)

    assert found.latest_deployment is None


async def test_search_services_returns_latest_deployment_request(setup) -> None:
    s, service = setup
    detail = await _create(service, s)
    for sha in ("a" * 40, "b" * 40):
        request = DeploymentRequest(
            service_id=detail.service.id,
            environment=Environment.PROD,
            source_sha=sha,
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=sha,
        )
        s.deployments.requests.append(request)
        request.id = len(s.deployments.requests)

    found = await service.search_services(OWNER, s.project_id)

    assert found[0].latest_deployment is not None
    assert found[0].latest_deployment.source_sha == "b" * 40


def _scaling(replicas: int) -> dict[str, object]:
    config = ScalingConfig.defaults().model_dump(mode="json")
    config["replicas"] = replicas
    return config


async def test_create_service_defaults_deployment_strategy_to_rolling(setup) -> None:
    s, service = setup

    detail = await _create(service, s)

    assert detail.service.deployment_strategy == DeploymentStrategy.ROLLING


@pytest.mark.parametrize("scaling_config", [None, _scaling(1), _scaling(0)])
async def test_update_service_canary_below_two_replicas_raises_invalid_input_and_keeps_value(
    setup, scaling_config: dict[str, object] | None
) -> None:
    s, service = setup
    detail = await _create(service, s)
    detail.service.scaling_config = scaling_config
    commits_before = s.session.commit_count

    with pytest.raises(InvalidInputError) as error:
        await service.update_service(
            OWNER, detail.service.id, {"deployment_strategy": DeploymentStrategy.CANARY}
        )

    assert [(i.field, i.reason) for i in error.value.issues] == [
        ("deploymentStrategy", "at_least_two_replicas_required")
    ]
    assert detail.service.deployment_strategy == DeploymentStrategy.ROLLING
    assert s.session.commit_count == commits_before


@pytest.mark.parametrize("strategy", [DeploymentStrategy.CANARY, DeploymentStrategy.BLUE_GREEN])
async def test_update_service_progressive_strategy_with_two_replicas_saves_without_deployment(
    setup, strategy: DeploymentStrategy
) -> None:
    s, service = setup
    detail = await _create(service, s)
    detail.service.scaling_config = _scaling(2)

    updated = await service.update_service(
        OWNER, detail.service.id, {"deployment_strategy": strategy}
    )

    assert updated.service.deployment_strategy == strategy
    assert s.deployments.requests == []


async def test_update_service_canary_with_flag_off_raises_invalid_input() -> None:
    s = Setup()
    service = await s.build(deployment_strategy_enabled=False)
    detail = await _create(service, s)
    detail.service.scaling_config = _scaling(3)

    with pytest.raises(InvalidInputError) as error:
        await service.update_service(
            OWNER, detail.service.id, {"deployment_strategy": DeploymentStrategy.CANARY}
        )

    assert [(i.field, i.reason) for i in error.value.issues] == [
        ("deploymentStrategy", "deployment_strategy_disabled")
    ]
    assert detail.service.deployment_strategy == DeploymentStrategy.ROLLING


async def test_update_service_rolling_with_flag_off_saves() -> None:
    s = Setup()
    service = await s.build(deployment_strategy_enabled=False)
    detail = await _create(service, s)
    detail.service.deployment_strategy = DeploymentStrategy.CANARY

    updated = await service.update_service(
        OWNER, detail.service.id, {"deployment_strategy": DeploymentStrategy.ROLLING}
    )

    assert updated.service.deployment_strategy == DeploymentStrategy.ROLLING


async def test_update_service_keeps_saved_canary_after_scale_down(setup) -> None:
    # 저장된 뒤 Pod 를 줄인 것은 막지 않는다. 같은 값을 다시 보내도 거절하지 않는다.
    s, service = setup
    detail = await _create(service, s)
    detail.service.deployment_strategy = DeploymentStrategy.CANARY
    detail.service.scaling_config = _scaling(1)

    updated = await service.update_service(
        OWNER,
        detail.service.id,
        {"deployment_strategy": DeploymentStrategy.CANARY, "port": 3000},
    )

    assert updated.service.deployment_strategy == DeploymentStrategy.CANARY
    assert updated.service.port == 3000
