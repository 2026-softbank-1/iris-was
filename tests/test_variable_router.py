from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.dependencies import get_current_user, get_variable_service
from app.main import app
from app.models.user import User
from tests.fakes_variable import OWNER, STRANGER, VariableSetup


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


@pytest.fixture
async def setup() -> VariableSetup:
    return await VariableSetup().build()


@pytest.fixture
async def client(setup: VariableSetup) -> AsyncIterator[AsyncClient]:
    current = {"user": _user(OWNER)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_variable_service] = setup.variable_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.current = current  # type: ignore[attr-defined]
        yield http
    app.dependency_overrides.clear()


def _url(setup: VariableSetup, suffix: str = "") -> str:
    return f"/api/v1/services/{setup.service.id}/variables{suffix}"


async def test_search_variables_empty_returns_system_variables_only(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.get(_url(setup))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["variables"] == []
    port = next(v for v in data["systemVariables"] if v["key"] == "PORT")
    assert port["value"] == "8080"
    target = next(v for v in data["systemVariables"] if v["key"] == "IRIS_TARGET_NAME")
    assert "value" not in target


async def test_create_variable_returns_201_and_shows_in_list(
    client: AsyncClient, setup: VariableSetup
) -> None:
    created = await client.post(_url(setup), json={"key": "DATABASE_URL", "value": "pg://x"})
    listed = await client.get(_url(setup))

    assert created.status_code == 201
    assert created.json()["data"] == {"key": "DATABASE_URL", "value": "pg://x"}
    assert listed.json()["data"]["variables"] == [{"key": "DATABASE_URL", "value": "pg://x"}]


async def test_create_variable_duplicate_returns_409(
    client: AsyncClient, setup: VariableSetup
) -> None:
    await client.post(_url(setup), json={"key": "A", "value": "1"})

    response = await client.post(_url(setup), json={"key": "A", "value": "2"})

    assert response.status_code == 409
    assert response.json()["code"] == "VARIABLE_CONFLICT"


@pytest.mark.parametrize("key", ["PORT", "IRIS_X", "1BAD", "has space"])
async def test_create_variable_invalid_key_returns_422(
    client: AsyncClient, setup: VariableSetup, key: str
) -> None:
    response = await client.post(_url(setup), json={"key": key, "value": "v"})

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_create_variable_missing_value_returns_422(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.post(_url(setup), json={"key": "A"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_update_variable_changes_value(client: AsyncClient, setup: VariableSetup) -> None:
    await client.post(_url(setup), json={"key": "A", "value": "old"})

    response = await client.put(_url(setup, "/A"), json={"value": "new"})

    assert response.status_code == 200
    assert response.json()["data"] == {"key": "A", "value": "new"}


async def test_update_variable_missing_returns_404(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.put(_url(setup, "/NOPE"), json={"value": "x"})

    assert response.status_code == 404
    assert response.json()["code"] == "VARIABLE_NOT_FOUND"


async def test_delete_variable_returns_204_then_404(
    client: AsyncClient, setup: VariableSetup
) -> None:
    await client.post(_url(setup), json={"key": "A", "value": "1"})

    first = await client.delete(_url(setup, "/A"))
    second = await client.delete(_url(setup, "/A"))

    assert first.status_code == 204
    assert second.status_code == 404


async def test_replace_variables_saves_raw_text_and_drops_missing_keys(
    client: AsyncClient, setup: VariableSetup
) -> None:
    await client.post(_url(setup), json={"key": "OLD", "value": "x"})

    response = await client.put(
        _url(setup), json={"raw": 'SESSION_SECRET="s3cret"\nLOG_LEVEL=info\n'}
    )

    assert response.status_code == 200
    assert response.json()["data"]["variables"] == [
        {"key": "LOG_LEVEL", "value": "info"},
        {"key": "SESSION_SECRET", "value": "s3cret"},
    ]
    listed = await client.get(_url(setup))
    assert [v["key"] for v in listed.json()["data"]["variables"]] == ["LOG_LEVEL", "SESSION_SECRET"]


async def test_replace_variables_invalid_line_returns_422_with_line(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.put(_url(setup), json={"raw": "A=1\nbroken line\n"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert body["message"] == "invalid variable line"
    assert body["details"] == [{"field": "raw", "reason": "line 2: invalid variable line"}]


async def test_replace_variables_reserved_keys_return_422_with_each_line(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.put(_url(setup), json={"raw": "A=1\nPORT=3000\nIRIS_X=y\n"})

    assert response.status_code == 422
    body = response.json()
    assert body["message"] == "variable key is reserved by the platform"
    assert body["details"] == [
        {"field": "raw", "reason": "line 2: reserved key PORT"},
        {"field": "raw", "reason": "line 3: reserved key IRIS_X"},
    ]
    assert (await client.get(_url(setup))).json()["data"]["variables"] == []


async def test_replace_variables_multiline_quoted_value_round_trips(
    client: AsyncClient, setup: VariableSetup
) -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIIFAKE\n-----END PRIVATE KEY-----"

    response = await client.put(
        _url(setup), json={"raw": f'BEFORE=1\nPRIVATE_KEY="{pem}"\nAFTER=2\n'}
    )

    assert response.status_code == 200
    assert response.json()["data"]["variables"] == [
        {"key": "AFTER", "value": "2"},
        {"key": "BEFORE", "value": "1"},
        {"key": "PRIVATE_KEY", "value": pem},
    ]


async def test_create_variable_invalid_key_has_no_details(
    client: AsyncClient, setup: VariableSetup
) -> None:
    response = await client.post(_url(setup), json={"key": "PORT", "value": "1"})

    assert response.status_code == 422
    assert "details" not in response.json()


async def test_variables_of_other_users_service_return_404(
    client: AsyncClient, setup: VariableSetup
) -> None:
    client.current["user"] = _user(STRANGER)  # type: ignore[attr-defined]

    responses = [
        await client.get(_url(setup)),
        await client.post(_url(setup), json={"key": "A", "value": "1"}),
        await client.put(_url(setup), json={"raw": "A=1"}),
        await client.put(_url(setup, "/A"), json={"value": "1"}),
        await client.delete(_url(setup, "/A")),
    ]

    assert [r.status_code for r in responses] == [404] * 5
    assert {r.json()["code"] for r in responses} == {"SERVICE_NOT_FOUND"}


async def test_variables_without_encryption_key_return_503(setup: VariableSetup) -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(OWNER)
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.get(_url(setup))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
