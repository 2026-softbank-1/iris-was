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
