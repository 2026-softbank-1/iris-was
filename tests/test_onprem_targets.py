"""등록한 온프레미스 서버가 타깃·도메인·배포 요청에 미치는 영향."""

import re

import pytest

from app.core.exceptions import InvalidInputError, TargetNotConnectedError
from app.enums import DeploymentTrigger, OnpremServerStatus, TargetKind
from app.models.deployment_request import DeploymentRequest
from app.models.onprem_server import OnpremServer
from app.models.target import Target
from app.schemas.service import TargetResponse
from app.services.deployment_request_service import DeploymentRequestService
from app.services.domain_service import build_service_host, service_host_label, target_server_key
from app.services.target_service import TargetService
from app.services.webhook_service import WebhookService
from tests.fakes import FakeGithubInstallationRepository, FakeSession
from tests.fakes_deployment import OWNER, DeploymentSetup
from tests.fakes_project import FakeTargetRepository
from tests.fakes_variable import FakeServiceVariableRepository
from tests.fakes_webhook import (
    FakeBuildRepository,
    FakeDeploymentRequestRepository,
    FakeDeploymentStatusHistoryRepository,
    FakeJobRepository,
    FakeWebhookServiceRepository,
)
from tests.test_service_registry_service import Setup as RegistrySetup
from tests.test_service_registry_service import _create
from tests.test_webhook_service import SECRET, _push, _service, _signed

# iris-infra 게이트웨이가 서버를 고르는 정규식(계약 §2).
GATEWAY_HOST = re.compile(
    r"^(?P<label>[a-z0-9-]+)-(?P<key>[a-z][a-z0-9]{7})\.internal\.likelion\.uk$"
)
SERVER_KEY = "k3x9q2ma"


def _server(status: OnpremServerStatus, *, server_id: int = 3, target_id: int = 7) -> OnpremServer:
    server = OnpremServer(
        owner_id=OWNER, name="home-lab", server_key=SERVER_KEY, target_id=target_id, status=status
    )
    server.id = server_id
    return server


def _owned_target(target_id: int, owner_id: int, server: OnpremServer | None = None) -> Target:
    target = Target(
        name=f"onprem-k{target_id:07d}",
        kind=TargetKind.ONPREM,
        domain_suffix="internal.likelion.uk",
        owner_id=owner_id,
        is_deleted=False,
    )
    target.id = target_id
    target.onprem_server = server
    return target


def test_service_host_label_for_server_target_appends_server_key() -> None:
    assert service_host_label("api", 12, SERVER_KEY) == f"api-12-{SERVER_KEY}"
    assert service_host_label("api", 12) == "api-12"


def test_service_host_for_server_target_matches_gateway_pattern() -> None:
    host = build_service_host("api", 12, "internal.likelion.uk", SERVER_KEY)

    match = GATEWAY_HOST.fullmatch(host)
    assert match is not None
    assert match["key"] == SERVER_KEY
    assert host == f"api-12-{SERVER_KEY}.internal.likelion.uk"


def test_service_host_for_shared_target_never_matches_gateway_pattern() -> None:
    # 기존 서버 host 는 라벨 끝이 `-숫자` 라 게이트웨이의 서버 키 모양과 겹치지 않는다.
    assert GATEWAY_HOST.fullmatch(build_service_host("api", 12, "internal.likelion.uk")) is None


@pytest.mark.parametrize("service_id", [1, 123456789])
def test_service_host_label_for_server_target_shortens_name_within_dns_limit(
    service_id: int,
) -> None:
    label = service_host_label("a-" * 40, service_id, SERVER_KEY)

    assert len(label) <= 63
    assert label.endswith(f"-{service_id}-{SERVER_KEY}")
    assert "--" not in label
    assert GATEWAY_HOST.fullmatch(f"{label}.internal.likelion.uk")


def test_target_server_key_reads_loaded_server() -> None:
    assert target_server_key(_owned_target(7, OWNER, _server(OnpremServerStatus.CONNECTED))) == (
        SERVER_KEY
    )
    assert target_server_key(Target(name="aws", kind=TargetKind.AWS)) is None


def test_target_response_carries_server_id_and_connection_status() -> None:
    response = TargetResponse.from_model(
        _owned_target(7, OWNER, _server(OnpremServerStatus.REGISTERING))
    )
    shared = TargetResponse.from_model(Target(id=1, name="aws", kind=TargetKind.AWS))

    body = response.model_dump(by_alias=True, exclude_none=True)
    assert body["onpremServerId"] == 3
    assert body["onpremServerName"] == "home-lab"
    assert body["connectionStatus"] == "REGISTERING"
    assert "connectionStatus" not in shared.model_dump(by_alias=True, exclude_none=True)


async def test_search_targets_shows_shared_and_own_servers_only() -> None:
    targets = FakeTargetRepository()
    targets.targets += [_owned_target(7, OWNER), _owned_target(8, 99)]
    deleted = _owned_target(9, OWNER)
    deleted.is_deleted = True
    targets.targets.append(deleted)

    found = await TargetService(targets).search_targets(OWNER)  # type: ignore[arg-type]

    assert [t.id for t in found] == [1, 2, 7]


async def test_create_service_on_other_owners_server_target_is_rejected_like_unknown() -> None:
    setup = RegistrySetup()
    registry = await setup.build()
    deleted = _owned_target(9, OWNER)
    deleted.is_deleted = True
    setup.targets.targets += [_owned_target(7, OWNER), _owned_target(8, 99), deleted]
    with pytest.raises(InvalidInputError) as unknown:
        await _create(registry, setup, name="unknown", target_ids=[404])

    detail = await _create(registry, setup, target_ids=[7])
    assert detail.target_ids == [7]
    for index, target_id in enumerate([8, 9]):
        with pytest.raises(InvalidInputError) as error:
            await _create(registry, setup, name=f"other-{index}", target_ids=[target_id])
        assert (error.value.code, error.value.message) == (
            unknown.value.code,
            unknown.value.message,
        )
        assert error.value.message == "unknown target"


@pytest.fixture
async def deployment() -> DeploymentSetup:
    return await DeploymentSetup().build()


@pytest.mark.parametrize(
    "status",
    [OnpremServerStatus.PENDING, OnpremServerStatus.REGISTERING, OnpremServerStatus.FAILED],
)
async def test_manual_deployment_to_unconnected_server_is_rejected(
    deployment: DeploymentSetup, status: OnpremServerStatus
) -> None:
    deployment.services.servers[deployment.service.id] = _server(status)

    with pytest.raises(TargetNotConnectedError) as error:
        await deployment.manual_service().create_deployment_request(
            OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL
        )

    assert error.value.status_code == 409
    assert error.value.code == "TARGET_NOT_CONNECTED"
    assert deployment.requests.requests == []


async def test_manual_deployment_to_connected_server_is_created(
    deployment: DeploymentSetup,
) -> None:
    deployment.services.servers[deployment.service.id] = _server(OnpremServerStatus.CONNECTED)

    request = await deployment.manual_service().create_deployment_request(
        OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL
    )

    assert request.id is not None


async def test_retry_with_same_key_returns_existing_request_even_if_server_disconnected(
    deployment: DeploymentSetup,
) -> None:
    server = _server(OnpremServerStatus.CONNECTED)
    deployment.services.servers[deployment.service.id] = server
    first = await deployment.manual_service().create_deployment_request(
        OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL, idempotency_key="k1"
    )
    server.status = OnpremServerStatus.REGISTERING

    replayed = await deployment.manual_service().create_deployment_request(
        OWNER, deployment.service.id, trigger_type=DeploymentTrigger.MANUAL, idempotency_key="k1"
    )

    assert replayed.id == first.id
    assert len(deployment.requests.requests) == 1


async def test_push_skips_service_on_unconnected_server_but_deploys_others() -> None:
    services = [_service(1), _service(2)]
    repository = FakeWebhookServiceRepository(services)
    repository.servers[1] = _server(OnpremServerStatus.PENDING)
    requests = FakeDeploymentRequestRepository()
    webhook = WebhookService(
        FakeSession(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeGithubInstallationRepository(),  # type: ignore[arg-type]
        DeploymentRequestService(
            requests,  # type: ignore[arg-type]
            FakeJobRepository(),  # type: ignore[arg-type]
            FakeDeploymentStatusHistoryRepository(),  # type: ignore[arg-type]
            FakeBuildRepository(),  # type: ignore[arg-type]
            FakeServiceVariableRepository(),  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
        ),
        SECRET,
    )
    body, signature = _signed(_push())

    receipt = await webhook.receive_github_event(
        event="push", delivery_id="d-1", signature=signature, body=body
    )

    assert [r.service_id for r in requests.requests] == [2]
    assert receipt.deployment_request_ids == [requests.requests[0].id]


async def test_removal_request_is_allowed_while_server_is_not_connected(
    deployment: DeploymentSetup,
) -> None:
    deployment.services.servers[deployment.service.id] = _server(OnpremServerStatus.FAILED)
    live = DeploymentRequest(id=99, source_sha="a" * 40, variables_snapshot={})

    request = await deployment.deployment_request_service().create_removal_request(
        deployment.service, source_deployment_request=live, idempotency_key="remove-1"
    )

    assert request is not None
    assert request.trigger_type == DeploymentTrigger.REMOVE
