import httpx
import pytest

from app.clients.oauth_client import GithubOAuthClient
from app.core.exceptions import ExternalError, UnauthorizedError


def _client(handler: httpx.MockTransport) -> GithubOAuthClient:
    return GithubOAuthClient(
        httpx.AsyncClient(transport=handler),
        client_id="cid",
        client_secret="secret",
        web_base_url="https://github.test",
        api_base_url="https://api.github.test",
    )


def test_build_authorization_url_contains_client_id_and_state() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200)))

    url = client.build_authorization_url("st")

    assert url == "https://github.test/login/oauth/authorize?client_id=cid&state=st"


async def test_exchange_code_returns_access_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(200, json={"access_token": "tok"})

    assert await _client(httpx.MockTransport(handler)).exchange_code("code") == "tok"


async def test_exchange_code_with_error_body_raises_unauthorized() -> None:
    handler = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": "bad_verification_code"})
    )

    with pytest.raises(UnauthorizedError):
        await _client(handler).exchange_code("code")


async def test_fetch_user_maps_github_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"id": 5, "login": "octo", "avatar_url": None})

    user = await _client(httpx.MockTransport(handler)).fetch_user("tok")

    assert (user.github_id, user.login, user.avatar_url) == (5, "octo", None)


async def test_fetch_installations_reads_every_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 2
        start = (page - 1) * 100
        items = [
            {"id": start + n, "account": {"login": f"acc{start + n}", "type": "Organization"}}
            for n in range(count)
        ]
        return httpx.Response(200, json={"installations": items})

    installations = await _client(httpx.MockTransport(handler)).fetch_installations("tok")

    assert len(installations) == 102
    assert installations[-1].account_type == "Organization"


async def test_rejected_token_raises_unauthorized() -> None:
    handler = httpx.MockTransport(lambda request: httpx.Response(401, json={}))

    with pytest.raises(UnauthorizedError):
        await _client(handler).fetch_user("tok")


async def test_server_error_raises_external_error_without_leaking_token() -> None:
    handler = httpx.MockTransport(lambda request: httpx.Response(500, json={}))

    with pytest.raises(ExternalError) as exc_info:
        await _client(handler).fetch_user("tok-secret")

    assert "tok-secret" not in str(exc_info.value)
    assert exc_info.value.fields == {"status_code": 500}


async def test_network_failure_raises_external_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ExternalError):
        await _client(httpx.MockTransport(handler)).fetch_user("tok")
