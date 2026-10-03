import pytest

from app.clients.source_repository_client import BranchInfo
from app.core.exceptions import InvalidInputError, RepositoryNotAccessibleError
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)


async def _service(
    repositories: dict[int, list], user_installations: list[tuple[int, str]]
) -> tuple[SourceRepositoryService, FakeSourceRepositoryClient]:
    installations = FakeGithubInstallationRepository()
    for index, (installation_id, login) in enumerate(user_installations, start=1):
        await installations.save(make_installation(index, installation_id, login))
    await installations.replace_user_links(1, {i for i in range(1, len(user_installations) + 1)})
    client = FakeSourceRepositoryClient(repositories)
    return SourceRepositoryService(installations, client), client  # type: ignore[arg-type]


async def test_search_repositories_merges_installations_sorted_by_name() -> None:
    service, _ = await _service(
        {
            11: [make_repository("octocat/zeta"), make_repository("octocat/alpha")],
            22: [make_repository("iris-org/web")],
        },
        [(11, "octocat"), (22, "iris-org")],
    )

    candidates = await service.search_repositories(1)

    assert [c.full_name for c in candidates] == ["iris-org/web", "octocat/alpha", "octocat/zeta"]
    assert candidates[0].installation_id == 22


async def test_search_repositories_filters_by_query_case_insensitively() -> None:
    service, _ = await _service(
        {11: [make_repository("octocat/Alpha"), make_repository("octocat/beta")]},
        [(11, "octocat")],
    )

    candidates = await service.search_repositories(1, query="ALP")

    assert [c.full_name for c in candidates] == ["octocat/Alpha"]


async def test_search_repositories_limits_to_requested_installation() -> None:
    service, _ = await _service(
        {11: [make_repository("octocat/a")], 22: [make_repository("iris-org/b")]},
        [(11, "octocat"), (22, "iris-org")],
    )

    candidates = await service.search_repositories(1, installation_id=22)

    assert [c.full_name for c in candidates] == ["iris-org/b"]


async def test_search_repositories_ignores_installations_of_other_users() -> None:
    service, _ = await _service({11: [make_repository("octocat/a")]}, [(11, "octocat")])

    assert await service.search_repositories(user_id=2) == []


async def test_resolve_repository_returns_candidate_with_installation() -> None:
    service, _ = await _service({22: [make_repository("iris-org/web")]}, [(22, "iris-org")])

    candidate = await service.resolve_repository(1, "https://github.com/Iris-Org/web.git")

    assert (candidate.full_name, candidate.installation_id) == ("iris-org/web", 22)


async def test_resolve_repository_without_installation_for_owner_raises_forbidden() -> None:
    service, _ = await _service({11: [make_repository("octocat/a")]}, [(11, "octocat")])

    with pytest.raises(RepositoryNotAccessibleError):
        await service.resolve_repository(1, "https://github.com/someone-else/repo")


async def test_resolve_repository_not_granted_to_installation_raises_forbidden() -> None:
    service, _ = await _service({22: [make_repository("iris-org/web")]}, [(22, "iris-org")])

    with pytest.raises(RepositoryNotAccessibleError):
        await service.resolve_repository(1, "iris-org/secret")


async def test_resolve_repository_with_invalid_url_raises_invalid_input() -> None:
    service, _ = await _service({}, [])

    with pytest.raises(InvalidInputError):
        await service.resolve_repository(1, "https://example.com/x")


async def test_search_branches_returns_branches_of_accessible_repository() -> None:
    service, client = await _service({22: [make_repository("iris-org/web")]}, [(22, "iris-org")])
    client.branches["iris-org/web"] = [BranchInfo("main", True), BranchInfo("dev", False)]

    branches = await service.search_branches(1, "iris-org/web")

    assert [b.name for b in branches] == ["main", "dev"]


async def test_create_clone_token_delegates_to_client() -> None:
    service, client = await _service({}, [])

    token = await service.create_clone_token(22)

    assert token.token == "ghs_fake"
    assert client.token_requests == [22]


async def test_create_repair_token_requires_user_link_and_current_repo_access() -> None:
    service, client = await _service({22: [make_repository("iris-org/web")]}, [(22, "iris-org")])
    token = await service.create_repair_token(1, "iris-org/web")
    assert token.token == "ghs_repair_fake"
    assert client.repair_token_requests == [(22, "iris-org/web")]
    for user_id, repository in [(2, "iris-org/web"), (1, "iris-org/private"), (1, "other/web")]:
        with pytest.raises(RepositoryNotAccessibleError):
            await service.create_repair_token(user_id, repository)
    assert len(client.repair_token_requests) == 1
