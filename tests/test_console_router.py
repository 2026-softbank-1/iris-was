from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.console_ticket import ConsoleTicketVerifier
from app.dependencies import get_console_service, get_current_user
from app.enums import TargetKind
from app.main import app
from app.models.user import User
from app.services.console_service import ConsoleGatewayAddress, ConsoleService
from tests.fakes import FakeSession
from tests.fakes_console import (
    FakeConsoleReleaseRepository,
    FakeConsoleServiceRepository,
    FakeConsoleSessionRepository,
    FakeConsoleTargetRepository,
    generate_ed25519_pem_pair,
)

OWNER_ID = 7
SERVICE_ID = 42
GATEWAY = ConsoleGatewayAddress("https://api.likelion.uk/console", "wss://api.likelion.uk/console")


class Harness:
    def __init__(self) -> None:
        self.private_pem, self.public_pem = generate_ed25519_pem_pair()
        self.services = FakeConsoleServiceRepository()
        self.services.add(SERVICE_ID, OWNER_ID, [1])
        self.targets = FakeConsoleTargetRepository()
        self.targets.add(1, TargetKind.AWS)
        self.targets.add(2, TargetKind.ONPREM)
        self.releases = FakeConsoleReleaseRepository()
        self.console_sessions = FakeConsoleSessionRepository()
        self.is_configured = True

    def build(self) -> ConsoleService:
        return ConsoleService(
            FakeSession(),  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.releases,  # type: ignore[arg-type]
            self.console_sessions,  # type: ignore[arg-type]
            self.private_pem if self.is_configured else None,
            GATEWAY if self.is_configured else None,
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    user = User(github_id=1007, login="owner")
    user.id = OWNER_ID
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_console_service] = harness.build
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http
    app.dependency_overrides.clear()


# ---- GET /console ---------------------------------------------------------------------------


async def test_get_console_availability_available(client: AsyncClient, harness: Harness) -> None:
    harness.releases.add(SERVICE_ID, 1, 5)

    response = await client.get(f"/api/v1/services/{SERVICE_ID}/console?targetId=1")

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"available": True}}


@pytest.mark.parametrize(
    ("target_id", "is_configured", "reason"),
    [
        (1, True, "NO_RUNNING_DEPLOYMENT"),
        (2, True, "TARGET_NOT_SUPPORTED"),
        (1, False, "NOT_CONFIGURED"),
    ],
)
async def test_get_console_availability_unavailable_reason(
    client: AsyncClient, harness: Harness, target_id: int, is_configured: bool, reason: str
) -> None:
    harness.services.target_ids[SERVICE_ID] = [target_id]
    harness.is_configured = is_configured

    response = await client.get(f"/api/v1/services/{SERVICE_ID}/console?targetId={target_id}")

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"available": False, "reason": reason}}


async def test_get_console_availability_unknown_service_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/services/999/console?targetId=1")

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_get_console_availability_unlinked_target_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/services/{SERVICE_ID}/console?targetId=2")

    assert response.status_code == 404


@pytest.mark.parametrize("query", ["", "?targetId=0", "?targetId=abc"])
async def test_get_console_availability_invalid_target_id_returns_422(
    client: AsyncClient, query: str
) -> None:
    response = await client.get(f"/api/v1/services/{SERVICE_ID}/console{query}")

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---- POST /console/sessions -----------------------------------------------------------------


async def test_create_console_session_returns_201_with_ticket(
    client: AsyncClient, harness: Harness
) -> None:
    harness.releases.add(SERVICE_ID, 1, 5)

    response = await client.post(
        f"/api/v1/services/{SERVICE_ID}/console/sessions", json={"targetId": 1}
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) == {"sessionId", "token", "expiresAt", "gateway"}
    assert data["gateway"] == {
        "httpUrl": "https://api.likelion.uk/console",
        "wsUrl": "wss://api.likelion.uk/console",
    }
    assert data["expiresAt"].endswith("Z") or "+00:00" in data["expiresAt"]
    claims = ConsoleTicketVerifier(harness.public_pem).verify(data["token"])
    assert claims.session_id == data["sessionId"]
    assert claims.namespace == f"svc-{SERVICE_ID}"
    assert len(harness.console_sessions.added) == 1


@pytest.mark.parametrize(
    ("target_id", "is_configured", "status_code", "code"),
    [
        (1, True, 409, "NO_RUNNING_DEPLOYMENT"),
        (2, True, 409, "CONSOLE_TARGET_NOT_SUPPORTED"),
        (1, False, 503, "NOT_CONFIGURED"),
    ],
)
async def test_create_console_session_unavailable_returns_error(
    client: AsyncClient,
    harness: Harness,
    target_id: int,
    is_configured: bool,
    status_code: int,
    code: str,
) -> None:
    harness.services.target_ids[SERVICE_ID] = [target_id]
    harness.is_configured = is_configured

    response = await client.post(
        f"/api/v1/services/{SERVICE_ID}/console/sessions", json={"targetId": target_id}
    )

    assert response.status_code == status_code
    assert response.json()["code"] == code
    assert harness.console_sessions.added == []


async def test_create_console_session_unknown_service_returns_404(client: AsyncClient) -> None:
    response = await client.post("/api/v1/services/999/console/sessions", json={"targetId": 1})

    assert response.status_code == 404


@pytest.mark.parametrize("body", [{}, {"targetId": 0}, {"targetId": "x"}])
async def test_create_console_session_invalid_body_returns_422(
    client: AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post(f"/api/v1/services/{SERVICE_ID}/console/sessions", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_console_endpoints_require_login() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        get_response = await http.get(f"/api/v1/services/{SERVICE_ID}/console?targetId=1")
        post_response = await http.post(
            f"/api/v1/services/{SERVICE_ID}/console/sessions", json={"targetId": 1}
        )

    assert get_response.status_code == 401
    assert post_response.status_code == 401
