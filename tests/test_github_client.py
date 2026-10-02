import json
from pathlib import Path

import httpx
import pytest

from app.clients.github_client import GitHubClient, SourceTooLargeError
from app.core.exceptions import ExternalError, ForbiddenError, GitOpsConflictError, NotFoundError


def _client(handler: httpx.MockTransport) -> GitHubClient:
    http = httpx.AsyncClient(transport=handler, base_url="https://api.github.com")
    return GitHubClient(http, app_id=1, private_key="unused")


async def test_download_tarball_follows_redirect_and_saves(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://codeload.github.com/t.tgz"})
        return httpx.Response(200, content=b"tarball")

    dest = tmp_path / "source.tar.gz"
    await _client(httpx.MockTransport(handle)).download_tarball("t", "o/r", "sha", dest, 100)

    assert dest.read_bytes() == b"tarball"


async def test_download_tarball_over_limit_raises_too_large(tmp_path: Path) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 11)))

    with pytest.raises(SourceTooLargeError):
        await client.download_tarball("t", "o/r", "sha", tmp_path / "s.tar.gz", 10)


@pytest.mark.parametrize(
    ("status_code", "headers", "expected"),
    [
        (404, {}, NotFoundError),
        (403, {}, ForbiddenError),
        (403, {"x-ratelimit-remaining": "0"}, ExternalError),
        (502, {}, ExternalError),
    ],
)
async def test_get_branch_sha_error_status_maps_to_app_error(
    status_code: int, headers: dict[str, str], expected: type[Exception]
) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(status_code, headers=headers)))

    with pytest.raises(expected):
        await client.get_branch_sha("t", "o/r", "main")


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(409, GitOpsConflictError), (422, GitOpsConflictError), (404, NotFoundError)],
)
async def test_update_branch_error_status_maps_to_conflict(
    status_code: int, expected: type[Exception]
) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(status_code)))

    with pytest.raises(expected):
        await client.update_branch("t", "o/r", "main", "sha")


@pytest.mark.parametrize(
    ("status", "expected"),
    [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)],
)
async def test_contains_compare_status_returns_ancestry(status: str, expected: bool) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"status": status})))

    assert await client.contains("t", "o/r", "target", "head") is expected


async def test_find_subtree_sha_walks_nested_path() -> None:
    trees = {
        "root": [{"path": "services", "type": "tree", "sha": "services-tree"}],
        "services-tree": [{"path": "12", "type": "tree", "sha": "service-12"}],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"tree": {"sha": "root"}})
        return httpx.Response(200, json={"tree": trees[request.url.path.rsplit("/", 1)[1]]})

    client = _client(httpx.MockTransport(handle))

    assert await client.find_subtree_sha("t", "o/r", "c1", "services/12") == "service-12"
    assert await client.find_subtree_sha("t", "o/r", "c1", "services/13") is None


async def test_create_delete_commit_sends_null_sha_tree_entry_and_returns_commit() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/git/commits/head"):
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "new-tree"})
        return httpx.Response(201, json={"sha": "new-commit"})

    client = _client(httpx.MockTransport(handle))

    sha = await client.create_delete_commit("t", "o/r", "head", "services/12/prod", "remove")

    assert sha == "new-commit"
    tree_request = next(body for _, path, body in requests if path.endswith("/git/trees"))
    assert tree_request == {
        "base_tree": "base-tree",
        "tree": [{"path": "services/12/prod", "sha": None, "mode": "040000", "type": "tree"}],
    }
    commit_request = next(body for _, path, body in requests if path.endswith("/git/commits"))
    assert commit_request == {"message": "remove", "tree": "new-tree", "parents": ["head"]}


async def test_create_delete_commit_missing_path_maps_to_not_found() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/commits/head"):
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        return httpx.Response(422, json={"message": "GitRPC::BadObjectState"})

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(NotFoundError):
        await client.create_delete_commit("t", "o/r", "head", "services/12/prod", "remove")
