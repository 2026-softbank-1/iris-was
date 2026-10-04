import hashlib
import hmac
import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.dependencies import get_webhook_service
from app.main import app
from app.models.service import Service
from app.services.deployment_request_service import DeploymentRequestService
from app.services.webhook_service import WebhookService
from tests.fakes import FakeGithubInstallationRepository, FakeSession
from tests.fakes_variable import FakeServiceVariableRepository
from tests.fakes_webhook import (
    FakeBuildRepository,
    FakeDeploymentRequestRepository,
    FakeDeploymentStatusHistoryRepository,
    FakeJobRepository,
    FakeWebhookServiceRepository,
)

SECRET = "webhook-secret"
REPO_URL = "https://github.com/20-s-Guys/IdealChat"


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _headers(body: bytes, event: str = "push", delivery_id: str = "d-1") -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _signature(body),
        "Content-Type": "application/json",
    }


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    service = Service(
        id=1,
        name="idealchat",
        source_repository_url=REPO_URL,
        source_branch="main",
        is_auto_deploy=True,
        is_deleted=False,
    )
    webhook_service = WebhookService(
        FakeSession(),  # type: ignore[arg-type]
        FakeWebhookServiceRepository([service]),  # type: ignore[arg-type]
        FakeGithubInstallationRepository(),  # type: ignore[arg-type]
        DeploymentRequestService(
            FakeDeploymentRequestRepository(),  # type: ignore[arg-type]
            FakeJobRepository(),  # type: ignore[arg-type]
            FakeDeploymentStatusHistoryRepository(),  # type: ignore[arg-type]
            FakeBuildRepository(),  # type: ignore[arg-type]
            FakeServiceVariableRepository(),  # type: ignore[arg-type]
            FakeWebhookServiceRepository([service]),  # type: ignore[arg-type]
        ),
        SECRET,
    )
    app.dependency_overrides[get_webhook_service] = lambda: webhook_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http
    app.dependency_overrides.clear()


PUSH = {
    "ref": "refs/heads/main",
    "after": "a" * 40,
    "head_commit": {"message": "feat: x"},
    "repository": {"html_url": REPO_URL},
}


async def test_push_webhook_returns_created_deployment_request_ids(client: AsyncClient) -> None:
    body = json.dumps(PUSH).encode()

    response = await client.post("/api/v1/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"isHandled": True, "deploymentRequestIds": [1], "repositoryAnalysisIds": []},
    }


async def test_webhook_with_wrong_signature_returns_unauthorized(client: AsyncClient) -> None:
    body = json.dumps(PUSH).encode()
    headers = _headers(body) | {"X-Hub-Signature-256": "sha256=" + "0" * 64}

    response = await client.post("/api/v1/webhooks/github", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


async def test_webhook_without_signature_returns_unauthorized(client: AsyncClient) -> None:
    body = json.dumps(PUSH).encode()
    headers = _headers(body)
    del headers["X-Hub-Signature-256"]

    response = await client.post("/api/v1/webhooks/github", content=body, headers=headers)

    assert response.status_code == 401


async def test_webhook_without_event_header_returns_validation_error(client: AsyncClient) -> None:
    body = b"{}"
    headers = _headers(body)
    del headers["X-GitHub-Event"]

    response = await client.post("/api/v1/webhooks/github", content=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_ping_webhook_is_acknowledged(client: AsyncClient) -> None:
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()

    response = await client.post(
        "/api/v1/webhooks/github", content=body, headers=_headers(body, event="ping")
    )

    assert response.status_code == 200
    assert response.json()["data"]["isHandled"] is False


async def test_webhook_without_secret_setting_returns_not_configured() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://t:t@127.0.0.1:1/t",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.post(
                "/api/v1/webhooks/github",
                content=b"{}",
                headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
