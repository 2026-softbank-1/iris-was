"""등록한 온프레미스 서버의 하트비트(last_seen_at)와 계산한 DISCONNECTED 상태."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import OnpremServerNotConnectedError, TargetNotConnectedError
from app.enums import (
    DeploymentTrigger,
    OnpremServerConnectionStatus,
    OnpremServerStatus,
    TargetKind,
)
from app.models.onprem_server import OnpremServer
from app.models.target import Target
from app.schemas.service import TargetResponse
from app.services.target_service import TargetService
from tests.fakes_deployment import DeploymentSetup
from tests.fakes_onprem import OWNER, OnpremSetup
from tests.fakes_project import FakeTargetRepository

OFFLINE_AFTER = timedelta(seconds=180)
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _server(status: OnpremServerStatus, last_seen_at: datetime | None) -> OnpremServer:
    server = OnpremServer(
        owner_id=OWNER,
        name="home-lab",
        server_key="k3x9q2ma",
        target_id=7,
        status=status,
        last_seen_at=last_seen_at,
    )
    server.id = 3
    return server


@pytest.mark.parametrize(
    ("status", "last_seen_at", "expected"),
    [
        (OnpremServerStatus.CONNECTED, NOW - timedelta(seconds=60), "CONNECTED"),
        (OnpremServerStatus.CONNECTED, NOW - timedelta(seconds=180), "CONNECTED"),
        (OnpremServerStatus.CONNECTED, NOW - timedelta(seconds=181), "DISCONNECTED"),
        # 기능 전에 연결된 서버(하트비트 없음)는 CONNECTED 로 본다.
        (OnpremServerStatus.CONNECTED, None, "CONNECTED"),
        # 연결 전 상태는 하트비트와 상관없이 그대로다.
        (OnpremServerStatus.REGISTERING, NOW - timedelta(hours=1), "REGISTERING"),
        (OnpremServerStatus.FAILED, NOW - timedelta(hours=1), "FAILED"),
        (OnpremServerStatus.PENDING, None, "PENDING"),
    ],
)
def test_connection_status_reports_disconnected_only_for_stale_connected_server(
    status: OnpremServerStatus, last_seen_at: datetime | None, expected: str
) -> None:
    server = _server(status, last_seen_at)

    assert server.connection_status(NOW, OFFLINE_AFTER) == OnpremServerConnectionStatus(expected)
    assert server.status == status


async def _connected(setup: OnpremSetup) -> tuple[OnpremServer, str]:
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)
    registration.server.mark_as_connected(datetime.now(UTC))
    return registration.server, secret


async def test_registry_credentials_records_heartbeat_and_server_reconnects() -> None:
    setup = OnpremSetup()
    server, secret = await _connected(setup)
    server.last_seen_at = datetime.now(UTC) - timedelta(minutes=10)
    assert setup.service.connection_status(server) == OnpremServerConnectionStatus.DISCONNECTED

    await setup.service.issue_registry_credentials(secret)

    assert server.last_seen_at > datetime.now(UTC) - timedelta(seconds=5)
    assert setup.service.connection_status(server) == OnpremServerConnectionStatus.CONNECTED


async def test_heartbeat_is_recorded_before_not_connected_conflict() -> None:
    setup = OnpremSetup(has_ecr=False)
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)
    registration.server.last_seen_at = None
    commits = setup.session.commit_count

    with pytest.raises(OnpremServerNotConnectedError):
        await setup.service.issue_registry_credentials(secret)

    assert registration.server.last_seen_at is not None
    assert setup.session.commit_count == commits + 1


async def test_heartbeat_is_written_at_most_once_per_30_seconds() -> None:
    setup = OnpremSetup()
    server, secret = await _connected(setup)
    recent = datetime.now(UTC) - timedelta(seconds=10)
    server.last_seen_at = recent

    await setup.service.issue_registry_credentials(secret)

    assert server.last_seen_at == recent


async def test_connect_and_bootstrap_record_heartbeat() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    assert registration.server.last_seen_at is None

    await setup.service.bootstrap(registration.registration_token)
    assert registration.server.last_seen_at is not None

    registration.server.last_seen_at = None
    await setup.connect(registration.registration_token, registration.server)
    assert registration.server.last_seen_at is not None


def test_target_service_reports_disconnected_server_target() -> None:
    server = _server(OnpremServerStatus.CONNECTED, datetime.now(UTC) - timedelta(minutes=5))
    target = Target(name="onprem-k3x9q2ma", kind=TargetKind.ONPREM, owner_id=OWNER)
    target.id = 7
    target.onprem_server = server
    service = TargetService(FakeTargetRepository(), offline_after=OFFLINE_AFTER)  # type: ignore[arg-type]

    status = service.connection_status(target)
    body = TargetResponse.from_model(target, status).model_dump(by_alias=True, exclude_none=True)

    assert body["connectionStatus"] == "DISCONNECTED"
    assert service.connection_status(Target(name="aws", kind=TargetKind.AWS)) is None


@pytest.fixture
async def deployment() -> DeploymentSetup:
    return await DeploymentSetup().build()


async def test_deployment_to_disconnected_server_is_rejected(deployment: DeploymentSetup) -> None:
    deployment.services.servers[deployment.service.id] = _server(
        OnpremServerStatus.CONNECTED, datetime.now(UTC) - timedelta(minutes=5)
    )

    with pytest.raises(TargetNotConnectedError):
        await deployment.manual_service().create_deployment_request(
            OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL
        )


async def test_deployment_to_connected_server_without_heartbeat_is_allowed(
    deployment: DeploymentSetup,
) -> None:
    deployment.services.servers[deployment.service.id] = _server(OnpremServerStatus.CONNECTED, None)

    request = await deployment.manual_service().create_deployment_request(
        OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.id is not None
