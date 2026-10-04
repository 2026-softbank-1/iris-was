import re
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
from tests.fakes_onprem import (
    INVALID_SERVER_NAMES,
    LEGACY_SERVER_NAMES,
    OWNER,
    VALID_SERVER_NAMES,
    OnpremSetup,
    name_ids,
)

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


async def test_create_server_same_name_is_409_name_conflict(env: Env) -> None:
    first = await env.client.post(BASE, json={"name": "e2e-dup"})
    second = await env.client.post(BASE, json={"name": "e2e-dup"})

    assert first.status_code == 201
    assert second.status_code == 409
    body = second.json()
    assert body["success"] is False
    assert body["code"] == "ONPREM_SERVER_NAME_CONFLICT"
    assert [s.name for s in env.setup.servers.servers] == ["e2e-dup"]


async def test_create_server_name_differing_only_by_surrounding_spaces_conflicts(
    env: Env,
) -> None:
    await env.client.post(BASE, json={"name": "home-lab"})

    response = await env.client.post(BASE, json={"name": " home-lab "})

    assert response.status_code == 409
    assert response.json()["code"] == "ONPREM_SERVER_NAME_CONFLICT"


@pytest.mark.parametrize(("name", "stored"), VALID_SERVER_NAMES, ids=name_ids(VALID_SERVER_NAMES))
async def test_create_server_with_valid_name_is_201_and_returns_it_trimmed(
    env: Env, name: str, stored: str
) -> None:
    response = await env.client.post(BASE, json={"name": name})

    assert response.status_code == 201
    assert response.json()["data"]["server"]["name"] == stored
    assert [s.name for s in env.setup.servers.servers] == [stored]


@pytest.mark.parametrize(
    ("name", "reason"), INVALID_SERVER_NAMES, ids=name_ids(INVALID_SERVER_NAMES)
)
async def test_create_server_with_invalid_name_is_422_invalid_input_with_details(
    env: Env, name: str, reason: str
) -> None:
    response = await env.client.post(BASE, json={"name": name})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "INVALID_INPUT"
    assert body["message"] == "invalid onprem server name"
    assert body["details"] == [{"field": "name", "reason": reason}]
    assert env.setup.servers.servers == []
    assert len(env.setup.targets.targets) == 2


def test_openapi_name_pattern_agrees_with_what_the_server_accepts() -> None:
    schema = app.openapi()["components"]["schemas"]["CreateOnpremServerRequest"]
    pattern = re.compile(schema["properties"]["name"]["pattern"])

    assert all(pattern.fullmatch(stored) for _, stored in VALID_SERVER_NAMES)
    assert not any(pattern.fullmatch(name.strip()) for name, _ in INVALID_SERVER_NAMES)


@pytest.mark.parametrize("body", [{}, {"name": None}, {"name": 123}, {"name": ["a"]}])
async def test_create_server_with_missing_or_non_string_name_is_422_validation_error(
    env: Env, body: dict[str, object]
) -> None:
    response = await env.client.post(BASE, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["details"][0]["field"] == "name"
    assert env.setup.servers.servers == []


async def test_create_server_conflict_is_checked_on_the_trimmed_name_both_ways(
    env: Env,
) -> None:
    first = await env.client.post(BASE, json={"name": " home-lab "})
    second = await env.client.post(BASE, json={"name": "home-lab"})
    third = await env.client.post(BASE, json={"name": "\thome-lab\n"})

    assert first.status_code == 201
    assert first.json()["data"]["server"]["name"] == "home-lab"
    assert second.status_code == 409
    assert second.json()["code"] == "ONPREM_SERVER_NAME_CONFLICT"
    assert third.status_code == 409
    assert [s.name for s in env.setup.servers.servers] == ["home-lab"]


@pytest.mark.parametrize("name", ["1", "0", "007", "2024", "1" * 63, " 12 "])
async def test_create_server_digits_only_name_is_422_with_one_reason(env: Env, name: str) -> None:
    response = await env.client.post(BASE, json={"name": name})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert body["details"] == [{"field": "name", "reason": "must not be only digits"}]
    assert env.setup.servers.servers == []


@pytest.mark.parametrize("name", ["1a", "a1", "1-2", "1.5", "007a"])
async def test_create_server_name_with_digits_and_other_characters_is_201(
    env: Env, name: str
) -> None:
    response = await env.client.post(BASE, json={"name": name})

    assert response.status_code == 201
    assert response.json()["data"]["server"]["name"] == name


async def test_create_server_internal_space_is_422_even_when_trimmed_name_exists(
    env: Env,
) -> None:
    await env.client.post(BASE, json={"name": "home-lab"})

    response = await env.client.post(BASE, json={"name": " home lab "})

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_create_server_names_differing_only_by_case_are_both_201(env: Env) -> None:
    upper = await env.client.post(BASE, json={"name": "Home-Lab"})
    lower = await env.client.post(BASE, json={"name": "home-lab"})
    again = await env.client.post(BASE, json={"name": "home-lab"})

    assert (upper.status_code, lower.status_code, again.status_code) == (201, 201, 409)


async def test_create_server_same_name_after_delete_is_201_again(env: Env) -> None:
    first = (await env.client.post(BASE, json={"name": "home-lab"})).json()["data"]["server"]
    deleted = await env.client.delete(f"{BASE}/{first['id']}")

    second = await env.client.post(BASE, json={"name": "home-lab"})

    assert deleted.status_code == 204
    assert second.status_code == 201
    assert second.json()["data"]["server"]["id"] != first["id"]


@pytest.mark.parametrize("legacy_name", LEGACY_SERVER_NAMES)
async def test_list_returns_server_registered_before_the_name_rule_as_is(
    env: Env, legacy_name: str
) -> None:
    created = (await env.client.post(BASE, json={"name": "legacy"})).json()["data"]["server"]
    env.setup.servers.servers[0].name = legacy_name

    listed = await env.client.get(BASE)
    fetched = await env.client.get(f"{BASE}/{created['id']}")
    reissued = await env.client.post(f"{BASE}/{created['id']}/registration-token")

    assert [s["name"] for s in listed.json()["data"]] == [legacy_name]
    assert fetched.json()["data"]["name"] == legacy_name
    assert reissued.status_code == 200
    assert reissued.json()["data"]["server"]["name"] == legacy_name


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
