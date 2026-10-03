from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.dependencies import (
    get_current_user,
    get_onprem_install_script_path,
    get_onprem_server_service,
)
from app.main import app
from app.models.user import User
from tests.fakes_onprem import OWNER, OnpremSetup

BASE = "/api/v1/onprem-servers"


@dataclass
class Env:
    client: AsyncClient
    setup: OnpremSetup


def _settings(api_base_url: str | None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t", api_base_url=api_base_url
    )


@pytest.fixture
def api_base_url() -> str | None:
    return "https://api.likelion.uk"


@pytest.fixture
async def env(tmp_path: Path, api_base_url: str | None) -> AsyncIterator[Env]:
    setup = OnpremSetup()
    user = User(github_id=1, login="owner")
    user.id = OWNER
    script = tmp_path / "install.sh"
    script.write_text("#!/usr/bin/env bash\necho install\n")
    app.dependency_overrides[get_settings] = lambda: _settings(api_base_url)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_onprem_server_service] = lambda: setup.service
    app.dependency_overrides[get_onprem_install_script_path] = lambda: script
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield Env(http, setup)
    app.dependency_overrides.clear()


async def test_install_script_is_served_as_shell_script(env: Env) -> None:
    response = await env.client.get(f"{BASE}/install.sh")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert response.text == "#!/usr/bin/env bash\necho install\n"


async def test_install_script_missing_returns_503(env: Env, tmp_path: Path) -> None:
    app.dependency_overrides[get_onprem_install_script_path] = lambda: tmp_path / "missing.sh"

    response = await env.client.get(f"{BASE}/install.sh")

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"


async def test_create_server_returns_token_and_install_command(env: Env) -> None:
    response = await env.client.post(BASE, json={"name": "home-lab"})

    assert response.status_code == 201
    data = response.json()["data"]
    token = data["registrationToken"]
    assert data["server"]["status"] == "PENDING"
    assert data["server"]["serverKey"]
    assert data["server"]["targetId"]
    assert data["installCommand"] == (
        "curl -fsSL https://api.likelion.uk/api/v1/onprem-servers/install.sh"
        f" | sudo bash -s -- --token {token}"
    )


@pytest.mark.parametrize("api_base_url", ["https://api.dev.test"])
async def test_install_command_names_api_url_when_not_default(env: Env) -> None:
    response = await env.client.post(BASE, json={"name": "home-lab"})

    command = response.json()["data"]["installCommand"]
    assert command.startswith("curl -fsSL https://api.dev.test/api/v1/onprem-servers/install.sh")
    assert command.endswith(" --api-url https://api.dev.test")


@pytest.mark.parametrize("api_base_url", [None, "http://api.dev.test"])
async def test_create_server_without_https_api_base_url_is_503_and_creates_nothing(
    env: Env,
) -> None:
    response = await env.client.post(BASE, json={"name": "home-lab"})

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
    assert env.setup.servers.servers == []
    assert len(env.setup.targets.targets) == 2


@pytest.mark.parametrize("api_base_url", ["http://localhost:8000"])
async def test_install_command_allows_local_http_api_base_url(env: Env) -> None:
    response = await env.client.post(BASE, json={"name": "home-lab"})

    assert response.status_code == 201
    assert response.json()["data"]["installCommand"].endswith(" --api-url http://localhost:8000")


async def test_create_server_blank_name_is_422(env: Env) -> None:
    response = await env.client.post(BASE, json={"name": "  "})

    assert response.status_code == 422


async def test_bootstrap_and_connect_flow_through_api(env: Env) -> None:
    created = (await env.client.post(BASE, json={"name": "home-lab"})).json()["data"]
    token = created["registrationToken"]

    bootstrap = await env.client.post(f"{BASE}/bootstrap", json={"registrationToken": token})
    assert bootstrap.status_code == 200
    data = bootstrap.json()["data"]
    key = data["serverKey"]
    assert data["tailscale"]["hostname"] == f"iris-{key}"
    assert data["versions"] == {
        "k3s": "v1.33.13+k3s2",
        "argoRollouts": "v1.10.0",
        "sealedSecrets": "0.40.0",
    }

    server = env.setup.servers.servers[0]
    secret = await env.setup.connect(token, server)
    assert secret
    listed = (await env.client.get(BASE)).json()["data"]
    assert [s["status"] for s in listed] == ["REGISTERING"]


async def test_bootstrap_with_bad_token_is_401(env: Env) -> None:
    response = await env.client.post(f"{BASE}/bootstrap", json={"registrationToken": "nope"})

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_REGISTRATION_TOKEN"


async def test_registry_credentials_before_connected_is_409(env: Env) -> None:
    created = (await env.client.post(BASE, json={"name": "home-lab"})).json()["data"]
    secret = await env.setup.connect(created["registrationToken"], env.setup.servers.servers[0])

    response = await env.client.post(
        f"{BASE}/registry-credentials", headers={"Authorization": f"Bearer {secret}"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ONPREM_SERVER_NOT_CONNECTED"


async def test_registry_credentials_without_bearer_is_401(env: Env) -> None:
    response = await env.client.post(f"{BASE}/registry-credentials")

    assert response.status_code == 401


async def test_reissue_and_delete_through_api(env: Env) -> None:
    created = (await env.client.post(BASE, json={"name": "home-lab"})).json()["data"]
    server_id = created["server"]["id"]

    reissued = await env.client.post(f"{BASE}/{server_id}/registration-token")
    assert reissued.status_code == 200
    assert reissued.json()["data"]["registrationToken"] != created["registrationToken"]

    deleted = await env.client.delete(f"{BASE}/{server_id}")
    assert deleted.status_code == 204
    assert (await env.client.get(f"{BASE}/{server_id}")).status_code == 404
