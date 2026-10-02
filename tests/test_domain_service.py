import pytest

from app.core.exceptions import ServiceNotFoundError
from app.models.project import Project
from app.models.service import Service
from app.services.domain_service import DomainService, build_service_host
from tests.fakes_domain import FakeReleaseRepository
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository, FakeTargetRepository

OWNER = 1
AWS_ID, LOCAL_ID = 1, 2


class Setup:
    def __init__(self) -> None:
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.targets = FakeTargetRepository()
        self.targets.targets[0].domain_suffix = "likelion.uk"  # local 은 규칙이 아직 없다
        self.releases = FakeReleaseRepository()
        self.service = DomainService(
            self.services,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.releases,  # type: ignore[arg-type]
        )

    async def add_service(
        self, name: str = "my-app", target_ids: set[int] | None = None
    ) -> Service:
        if not self.projects.projects:
            await self.projects.save(Project(name="p", owner_id=OWNER))
        service = await self.services.save(
            Service(
                project_id=1,
                name=name,
                source_repository_url="https://github.com/o/r",
                github_installation_id=1,
                source_branch="main",
            )
        )
        await self.services.replace_targets(service.id, target_ids or {AWS_ID, LOCAL_ID})
        return service


@pytest.fixture
def setup() -> Setup:
    return Setup()


@pytest.mark.parametrize(
    ("name", "service_id", "expected"),
    [
        ("my-app", 12, "my-app-12.likelion.uk"),
        ("My_App!", 3, "my-app-3.likelion.uk"),
        ("---", 7, "service-7.likelion.uk"),
    ],
)
def test_build_service_host_joins_label_and_suffix(
    name: str, service_id: int, expected: str
) -> None:
    assert build_service_host(name, service_id, "likelion.uk") == expected


def test_build_service_host_long_name_keeps_label_within_dns_limit() -> None:
    host = build_service_host("a" * 63, 123456, "likelion.uk")

    label = host.removesuffix(".likelion.uk")
    assert len(label) <= 63
    assert label.endswith("-123456")


async def test_search_domains_returns_host_per_linked_target(setup: Setup) -> None:
    service = await setup.add_service()

    details = await setup.service.search_domains(OWNER, service.id)

    assert [(d.target.name, d.host, d.is_connected) for d in details] == [
        ("aws", f"my-app-{service.id}.likelion.uk", False),
        ("local", None, False),
    ]


async def test_search_domains_only_lists_linked_targets(setup: Setup) -> None:
    service = await setup.add_service(target_ids={AWS_ID})

    details = await setup.service.search_domains(OWNER, service.id)

    assert [d.target.name for d in details] == ["aws"]


async def test_search_domains_marks_connected_after_successful_release(setup: Setup) -> None:
    service = await setup.add_service()
    setup.releases.connected.add((service.id, AWS_ID))

    details = await setup.service.search_domains(OWNER, service.id)

    assert [d.is_connected for d in details] == [True, False]


async def test_search_domains_of_other_owner_raises_not_found(setup: Setup) -> None:
    service = await setup.add_service()

    with pytest.raises(ServiceNotFoundError):
        await setup.service.search_domains(OWNER + 1, service.id)
