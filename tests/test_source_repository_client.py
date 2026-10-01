import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.exceptions import ExternalError, UnauthorizedError

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_KEY = _KEY.public_key()


def _client(
    handler: httpx.MockTransport, private_key: str = PRIVATE_PEM
) -> GithubSourceRepositoryClient:
    return GithubSourceRepositoryClient(
        httpx.AsyncClient(transport=handler), "12345", private_key, "https://api.github.test"
    )


def _repo(name: str) -> dict[str, object]:
    return {
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "default_branch": "main",
        "private": True,
    }


def _github(routes: dict[tuple[str, str], httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("POST", "/app/installations/9/access_tokens"):
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                PUBLIC_KEY,
                algorithms=["RS256"],
            )
            assert claims["iss"] == "12345"
            assert 0 < claims["exp"] - claims["iat"] <= 600 + 60
            return httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"}
            )
        assert request.headers["Authorization"] == "Bearer ghs_x"
        return routes.get(key, httpx.Response(404, json={}))

    return httpx.MockTransport(handler)


async def test_create_installation_token_signs_app_jwt_and_returns_token() -> None:
    token = await _client(_github({})).create_installation_token(9)

    assert token.token == "ghs_x"
    assert token.expires_at.year == 2030


async def test_private_key_with_escaped_newlines_is_accepted() -> None:
    escaped = PRIVATE_PEM.replace("\n", "\\n")

    token = await _client(_github({}), escaped).create_installation_token(9)

    assert token.token == "ghs_x"


async def test_invalid_private_key_raises_external_error_without_leaking_key() -> None:
    with pytest.raises(ExternalError) as exc_info:
        await _client(_github({}), "not-a-pem").create_installation_token(9)

    assert "not-a-pem" not in str(exc_info.value)


async def test_fetch_repositories_reads_every_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"}
            )
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 1
        return httpx.Response(
            200, json={"repositories": [_repo(f"o/r{page}-{n}") for n in range(count)]}
        )

    repositories = await _client(httpx.MockTransport(handler)).fetch_repositories(9)

    assert len(repositories) == 101
    assert repositories[0].is_private is True


async def test_find_repository_returns_none_when_not_found() -> None:
    client = _client(_github({}))

    assert await client.find_repository(9, "o/missing") is None


async def test_find_repository_returns_repository_info() -> None:
    client = _client(_github({("GET", "/repos/o/web"): httpx.Response(200, json=_repo("o/web"))}))

    repository = await client.find_repository(9, "o/web")

    assert repository is not None
    assert (repository.full_name, repository.default_branch) == ("o/web", "main")


async def test_fetch_branches_marks_default_branch() -> None:
    client = _client(
        _github(
            {
                ("GET", "/repos/o/web"): httpx.Response(200, json=_repo("o/web")),
                ("GET", "/repos/o/web/branches"): httpx.Response(
                    200, json=[{"name": "dev"}, {"name": "main"}]
                ),
            }
        )
    )

    branches = await client.fetch_branches(9, "o/web")

    assert [(b.name, b.is_default) for b in branches] == [("dev", False), ("main", True)]


async def test_unauthorized_app_credentials_raise_unauthorized() -> None:
    handler = httpx.MockTransport(lambda request: httpx.Response(401, json={}))

    with pytest.raises(UnauthorizedError):
        await _client(handler).create_installation_token(9)
