from datetime import UTC, datetime, timedelta

import pytest

from app.core.console_ticket import ConsoleTicketVerifier
from app.core.exceptions import (
    ConsoleTargetNotSupportedError,
    NoRunningDeploymentError,
    NotConfiguredError,
    NotFoundError,
    ServiceNotFoundError,
)
from app.enums import ConsoleUnavailableReason, TargetKind
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
AWS_TARGET_ID = 1
ONPREM_TARGET_ID = 2
GATEWAY = ConsoleGatewayAddress("https://api.likelion.uk/console", "wss://api.likelion.uk/console")


class Setup:
    def __init__(self, *, is_configured: bool = True, target_ids: list[int] | None = None) -> None:
        self.private_pem, self.public_pem = generate_ed25519_pem_pair()
        self.session = FakeSession()
        self.services = FakeConsoleServiceRepository()
        self.services.add(SERVICE_ID, OWNER_ID, target_ids or [AWS_TARGET_ID])
        self.targets = FakeConsoleTargetRepository()
        self.targets.add(AWS_TARGET_ID, TargetKind.AWS)
        self.targets.add(ONPREM_TARGET_ID, TargetKind.ONPREM)
        self.releases = FakeConsoleReleaseRepository()
        self.console_sessions = FakeConsoleSessionRepository()
        self.service = ConsoleService(
            self.session,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.releases,  # type: ignore[arg-type]
            self.console_sessions,  # type: ignore[arg-type]
            self.private_pem if is_configured else None,
            GATEWAY if is_configured else None,
        )

    def run_release(self, release_id: int = 5, target_id: int = AWS_TARGET_ID) -> None:
        self.releases.add(SERVICE_ID, target_id, release_id)


# ---- get_availability -----------------------------------------------------------------------


async def test_get_availability_running_release_is_available() -> None:
    setup = Setup()
    setup.run_release()

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert availability.is_available is True
    assert availability.reason is None
    assert setup.console_sessions.added == []
    assert setup.session.commit_count == 0


async def test_get_availability_without_release_reports_no_running_deployment() -> None:
    setup = Setup()

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert availability.is_available is False
    assert availability.reason == ConsoleUnavailableReason.NO_RUNNING_DEPLOYMENT


async def test_get_availability_onprem_target_reports_not_supported() -> None:
    setup = Setup(target_ids=[ONPREM_TARGET_ID])
    setup.run_release(target_id=ONPREM_TARGET_ID)

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, ONPREM_TARGET_ID)

    assert availability.reason == ConsoleUnavailableReason.TARGET_NOT_SUPPORTED


async def test_get_availability_without_configuration_reports_not_configured() -> None:
    setup = Setup(is_configured=False)
    setup.run_release()

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert availability.reason == ConsoleUnavailableReason.NOT_CONFIGURED


async def test_get_availability_target_kind_is_checked_before_configuration() -> None:
    setup = Setup(is_configured=False, target_ids=[ONPREM_TARGET_ID])

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, ONPREM_TARGET_ID)

    assert availability.reason == ConsoleUnavailableReason.TARGET_NOT_SUPPORTED


async def test_get_availability_unconfigured_is_checked_before_release() -> None:
    setup = Setup(is_configured=False)

    availability = await setup.service.get_availability(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert availability.reason == ConsoleUnavailableReason.NOT_CONFIGURED


@pytest.mark.parametrize("method", ["get_availability", "create_session"])
async def test_service_is_not_found_for_other_owner(method: str) -> None:
    setup = Setup()
    setup.run_release()

    with pytest.raises(ServiceNotFoundError):
        await getattr(setup.service, method)(OWNER_ID + 1, SERVICE_ID, AWS_TARGET_ID)
    with pytest.raises(ServiceNotFoundError):
        await getattr(setup.service, method)(OWNER_ID, SERVICE_ID + 1, AWS_TARGET_ID)


@pytest.mark.parametrize("method", ["get_availability", "create_session"])
async def test_target_not_linked_to_service_is_not_found(method: str) -> None:
    setup = Setup()
    setup.run_release()

    with pytest.raises(NotFoundError):
        await getattr(setup.service, method)(OWNER_ID, SERVICE_ID, ONPREM_TARGET_ID)
    with pytest.raises(NotFoundError):
        await getattr(setup.service, method)(OWNER_ID, SERVICE_ID, 999)


# ---- create_session -------------------------------------------------------------------------


async def test_create_session_signs_ticket_and_records_audit_row() -> None:
    setup = Setup()
    setup.run_release(release_id=5)
    before = datetime.now(UTC)

    issued = await setup.service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    claims = ConsoleTicketVerifier(setup.public_pem).verify(issued.token)
    assert claims.session_id == issued.session_id
    assert (claims.user_id, claims.service_id, claims.target_id) == (OWNER_ID, SERVICE_ID, 1)
    assert claims.namespace == f"svc-{SERVICE_ID}"
    assert claims.cluster == "aws"
    assert abs((issued.expires_at - claims.expires_at).total_seconds()) < 1
    assert before + timedelta(seconds=59) <= issued.expires_at <= before + timedelta(seconds=62)
    assert issued.gateway == GATEWAY
    (row,) = setup.console_sessions.added
    assert row.public_id == issued.session_id
    assert (row.user_id, row.service_id, row.target_id, row.release_id) == (
        OWNER_ID,
        SERVICE_ID,
        AWS_TARGET_ID,
        5,
    )
    assert row.expires_at == issued.expires_at
    assert setup.session.commit_count == 1


async def test_create_session_issues_distinct_session_ids() -> None:
    setup = Setup()
    setup.run_release()

    first = await setup.service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)
    second = await setup.service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert first.session_id != second.session_id
    assert first.token != second.token


async def test_create_session_without_release_raises_no_running_deployment() -> None:
    setup = Setup()

    with pytest.raises(NoRunningDeploymentError):
        await setup.service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert setup.console_sessions.added == []
    assert setup.session.commit_count == 0


async def test_create_session_onprem_target_raises_not_supported() -> None:
    setup = Setup(target_ids=[ONPREM_TARGET_ID])
    setup.run_release(target_id=ONPREM_TARGET_ID)

    with pytest.raises(ConsoleTargetNotSupportedError):
        await setup.service.create_session(OWNER_ID, SERVICE_ID, ONPREM_TARGET_ID)

    assert setup.console_sessions.added == []


async def test_create_session_without_configuration_raises_not_configured() -> None:
    setup = Setup(is_configured=False)
    setup.run_release()

    with pytest.raises(NotConfiguredError):
        await setup.service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert setup.console_sessions.added == []


async def test_create_session_with_invalid_private_key_raises_not_configured() -> None:
    setup = Setup()
    setup.run_release()
    service = ConsoleService(
        setup.session,  # type: ignore[arg-type]
        setup.services,  # type: ignore[arg-type]
        setup.targets,  # type: ignore[arg-type]
        setup.releases,  # type: ignore[arg-type]
        setup.console_sessions,  # type: ignore[arg-type]
        "not a pem",
        GATEWAY,
    )

    with pytest.raises(NotConfiguredError):
        await service.create_session(OWNER_ID, SERVICE_ID, AWS_TARGET_ID)

    assert setup.console_sessions.added == []
