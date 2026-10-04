import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    ExternalError,
    InvalidInputError,
    InvalidRegistrationTokenError,
    InvalidStatusTransitionError,
    NotConfiguredError,
    OnpremServerInUseError,
    OnpremServerLimitExceededError,
    OnpremServerNameConflictError,
    OnpremServerNotConnectedError,
    OnpremServerNotFoundError,
    UnauthorizedError,
)
from app.core.security import hash_url_token
from app.enums import OnpremServerFailureCode, OnpremServerStatus, TargetKind
from app.models.onprem_server import OnpremServer
from app.models.project import Project
from app.models.service import Service
from tests.fakes_onprem import (
    CA_PEM,
    OTHER_OWNER,
    OWNER,
    SEALED_SECRETS_CERT,
    TAILSCALE_AUTH_KEY,
    OnpremSetup,
)

SERVER_KEY_PATTERN = re.compile(r"[a-z][a-z0-9]{7}")


async def _attach_service(setup: OnpremSetup, target_id: int) -> Service:
    project = await setup.projects.save(Project(name=f"p{target_id}", owner_id=OWNER))
    service = await setup.services.save(
        Service(
            project_id=project.id,
            name="api",
            source_repository_url="https://github.com/o/r",
            github_installation_id=1,
            source_branch="main",
        )
    )
    await setup.services.replace_targets(service.id, {target_id})
    return service


async def test_create_server_creates_owned_target_and_hashes_token() -> None:
    setup = OnpremSetup()

    registration = await setup.service.create_server(OWNER, "home-lab")

    server = registration.server
    assert SERVER_KEY_PATTERN.fullmatch(server.server_key)
    assert server.status == OnpremServerStatus.PENDING
    assert len(registration.registration_token) == 43
    assert server.registration_token_hash == hash_url_token(registration.registration_token)
    assert registration.registration_token not in server.registration_token_hash
    remaining = server.registration_expires_at - datetime.now(UTC)
    assert timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24)
    target = next(t for t in setup.targets.targets if t.id == server.target_id)
    assert target.name == f"onprem-{server.server_key}"
    assert target.kind == TargetKind.ONPREM
    assert target.domain_suffix == "internal.likelion.uk"
    assert target.owner_id == OWNER
    assert setup.session.commit_count == 1


async def test_create_server_same_name_conflicts_but_other_owner_may_use_it() -> None:
    setup = OnpremSetup()
    await setup.service.create_server(OWNER, "home-lab")

    with pytest.raises(OnpremServerNameConflictError):
        await setup.service.create_server(OWNER, "home-lab")
    await setup.service.create_server(OTHER_OWNER, "home-lab")


async def test_create_server_conflict_error_fields_do_not_shadow_log_record_attributes() -> None:
    setup = OnpremSetup()
    await setup.service.create_server(OWNER, "home-lab")

    with pytest.raises(OnpremServerNameConflictError) as raised:
        await setup.service.create_server(OWNER, "home-lab")

    # 예외 핸들러가 fields 를 logging extra 로 넘긴다. 예약 속성과 겹치면 응답이 500 이 된다.
    reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    assert not reserved & set(raised.value.fields)


async def test_create_server_same_name_after_delete_is_allowed() -> None:
    setup = OnpremSetup()
    first = (await setup.service.create_server(OWNER, "home-lab")).server
    await setup.service.delete_server(OWNER, first.id)

    second = (await setup.service.create_server(OWNER, "home-lab")).server

    assert second.id != first.id
    assert second.target_id != first.target_id
    assert [s.id for s in await setup.service.search_servers(OWNER)] == [second.id]
    with pytest.raises(OnpremServerNameConflictError):
        await setup.service.create_server(OWNER, "home-lab")


async def test_create_server_concurrent_name_conflict_rolls_back_and_raises_conflict() -> None:
    setup = OnpremSetup()

    async def lose_the_race(server: OnpremServer) -> OnpremServer:
        # 사전 조회를 지난 뒤 다른 요청이 먼저 커밋해 유일 인덱스가 거절한 경우
        raise OnpremServerNameConflictError(
            "onprem server name already exists", owner_id=server.owner_id
        )

    setup.servers.save = lose_the_race  # type: ignore[method-assign]

    with pytest.raises(OnpremServerNameConflictError):
        await setup.service.create_server(OWNER, "home-lab")

    assert setup.session.rollback_count == 1
    assert setup.session.commit_count == 0


async def test_create_server_other_integrity_error_propagates_after_rollback() -> None:
    setup = OnpremSetup()
    error = IntegrityError("INSERT INTO onprem_servers ...", {}, Exception("server_key"))

    async def violate_other_constraint(server: OnpremServer) -> OnpremServer:
        raise error

    setup.servers.save = violate_other_constraint  # type: ignore[method-assign]

    with pytest.raises(IntegrityError) as raised:
        await setup.service.create_server(OWNER, "home-lab")

    assert raised.value is error
    assert setup.session.rollback_count == 1
    assert setup.session.commit_count == 0


async def test_get_server_of_other_owner_is_not_found() -> None:
    setup = OnpremSetup()
    server = (await setup.service.create_server(OWNER, "home-lab")).server

    with pytest.raises(OnpremServerNotFoundError):
        await setup.service.get_server(OTHER_OWNER, server.id)
    assert await setup.service.search_servers(OTHER_OWNER) == []


async def test_reissue_token_invalidates_previous_token() -> None:
    setup = OnpremSetup()
    first = await setup.service.create_server(OWNER, "home-lab")

    second = await setup.service.reissue_registration_token(OWNER, first.server.id)

    assert second.registration_token != first.registration_token
    with pytest.raises(InvalidRegistrationTokenError):
        await setup.service.bootstrap(first.registration_token)
    assert (await setup.service.bootstrap(second.registration_token)).server_key


async def test_reissue_token_from_failed_returns_to_pending() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    await setup.connect(registration.registration_token, registration.server)
    registration.server.fail(OnpremServerFailureCode.CONNECT_TIMED_OUT)

    reissued = await setup.service.reissue_registration_token(OWNER, registration.server.id)

    assert reissued.server.status == OnpremServerStatus.PENDING
    assert reissued.server.failure_code is None
    assert reissued.server.server_secret_hash is None
    assert reissued.server.next_check_at is None


async def test_reissue_token_while_registering_stops_the_worker_and_returns_to_pending() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server = registration.server
    await setup.connect(registration.registration_token, server)
    server.locked_by = "worker-1"
    server.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    server.confirm_gitops_commit(datetime.now(UTC) + timedelta(minutes=15))

    reissued = await setup.service.reissue_registration_token(OWNER, server.id)

    assert reissued.server.status == OnpremServerStatus.PENDING
    assert server.connect_generation == 2
    assert server.locked_by is None and server.locked_until is None
    assert server.connect_deadline_at is None and server.next_check_at is None
    assert server.server_secret_hash is None
    # 다시 connect 해야 하므로 저장한 접속 정보도 지운다.
    assert server.tailnet_fqdn is None
    assert server.api_ca_cert is None
    assert server.encrypted_service_account_token is None
    assert server.sealed_secrets_cert is None


async def test_reissue_token_when_connected_is_invalid_transition() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    registration.server.mark_as_connected(datetime.now(UTC))

    with pytest.raises(InvalidStatusTransitionError):
        await setup.service.reissue_registration_token(OWNER, registration.server.id)


async def test_create_server_over_owner_limit_is_rejected() -> None:
    setup = OnpremSetup()
    servers = [(await setup.service.create_server(OWNER, f"s{i}")).server for i in range(5)]

    with pytest.raises(OnpremServerLimitExceededError) as error:
        await setup.service.create_server(OWNER, "sixth")
    assert error.value.status_code == 409
    assert error.value.code == "ONPREM_SERVER_LIMIT_EXCEEDED"

    await setup.service.delete_server(OWNER, servers[0].id)
    assert await setup.service.create_server(OWNER, "sixth")
    await setup.service.create_server(OTHER_OWNER, "other")


async def test_bootstrap_returns_key_tailscale_and_versions() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")

    bootstrap = await setup.service.bootstrap(registration.registration_token)

    key = registration.server.server_key
    assert bootstrap.server_key == key
    assert bootstrap.tailscale_auth_key == TAILSCALE_AUTH_KEY
    assert bootstrap.tailscale_hostname == f"iris-{key}"
    assert bootstrap.tailscale_tags == ("tag:iris-onprem",)
    assert bootstrap.k3s_version == "v1.33.13+k3s2"


@pytest.mark.parametrize("case", ["unknown", "expired", "connected", "deleted"])
async def test_bootstrap_rejects_bad_tokens_alike(case: str) -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server, token = registration.server, registration.registration_token
    if case == "unknown":
        token = "x" * 43
    elif case == "expired":
        server.registration_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif case == "connected":
        server.mark_as_connected(datetime.now(UTC))
    else:
        await setup.service.delete_server(OWNER, server.id)

    with pytest.raises(InvalidRegistrationTokenError) as error:
        await setup.service.bootstrap(token)
    assert error.value.message == "invalid registration token"


async def test_bootstrap_without_tailscale_key_is_not_configured() -> None:
    setup = OnpremSetup(tailscale_auth_key=None)
    registration = await setup.service.create_server(OWNER, "home-lab")

    with pytest.raises(NotConfiguredError):
        await setup.service.bootstrap(registration.registration_token)


async def test_bootstrap_after_failure_with_same_token_is_allowed() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    registration.server.fail(OnpremServerFailureCode.CONNECT_TIMED_OUT)

    assert await setup.service.bootstrap(registration.registration_token)
    assert registration.server.status == OnpremServerStatus.FAILED


async def test_bootstrap_while_registering_is_allowed_without_status_change() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    await setup.connect(registration.registration_token, registration.server)

    assert await setup.service.bootstrap(registration.registration_token)
    assert registration.server.status == OnpremServerStatus.REGISTERING
    assert registration.server.connect_generation == 1


async def test_connect_after_failure_returns_to_registering_with_new_check() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server = registration.server
    await setup.connect(registration.registration_token, server)
    server.confirm_gitops_commit(datetime.now(UTC))
    server.fail(OnpremServerFailureCode.CONNECT_TIMED_OUT)

    await setup.connect(registration.registration_token, server)

    assert server.status == OnpremServerStatus.REGISTERING
    assert server.failure_code is None
    # 커밋이 다시 반영되면 기한을 새로 잡는다.
    assert server.connect_deadline_at is None
    assert server.next_check_at is not None


async def test_connect_stores_encrypted_token_and_starts_registering() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server = registration.server

    secret = await setup.connect(registration.registration_token, server)

    assert server.status == OnpremServerStatus.REGISTERING
    assert len(secret) == 43
    assert server.server_secret_hash == hash_url_token(secret)
    assert server.encrypted_service_account_token != "sa-token"
    assert setup.cipher.decrypt(server.encrypted_service_account_token or "") == "sa-token"
    assert server.tailnet_fqdn == f"iris-{server.server_key}.tailb046e8.ts.net"
    assert server.api_ca_cert == CA_PEM.strip() + "\n"
    assert server.connect_generation == 1
    assert server.next_check_at is not None
    assert server.connect_deadline_at is None


async def test_connect_again_overwrites_and_issues_new_secret() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server = registration.server
    first = await setup.connect(registration.registration_token, server)
    server.record_gitops_commit("c1")

    second = await setup.connect(registration.registration_token, server)

    assert second != first
    assert server.server_secret_hash == hash_url_token(second)
    assert server.connect_generation == 2
    assert server.gitops_commit_sha is None


async def test_connect_after_connected_is_rejected() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    await setup.connect(registration.registration_token, registration.server)
    registration.server.mark_as_connected(datetime.now(UTC))

    with pytest.raises(InvalidRegistrationTokenError):
        await setup.connect(registration.registration_token, registration.server)


@pytest.mark.parametrize(
    ("fqdn", "ca", "cert", "fields"),
    [
        ("iris-other000.tailb046e8.ts.net", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}.tail net.ts.net", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}.ts.net", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}.a.b.ts.net", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}.tailb046e8.example.com", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        ("{key}x.tailb046e8.ts.net", CA_PEM, SEALED_SECRETS_CERT, {"tailnetFqdn"}),
        (
            "{key}.tailb046e8.ts.net",
            "not a pem",
            "not a pem",
            {"apiCaCert", "sealedSecretsCert"},
        ),
    ],
)
async def test_connect_invalid_values_are_rejected_without_change(
    fqdn: str, ca: str, cert: str, fields: set[str]
) -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    server = registration.server

    with pytest.raises(InvalidInputError) as error:
        await setup.service.connect(
            registration.registration_token,
            tailnet_fqdn=fqdn.replace("{key}", f"iris-{server.server_key}"),
            api_ca_cert=ca,
            service_account_token="sa-token",
            sealed_secrets_cert=cert,
        )

    assert {issue.field for issue in error.value.issues} == fields
    assert server.status == OnpremServerStatus.PENDING
    assert server.server_secret_hash is None


async def test_connect_without_encryption_key_is_not_configured() -> None:
    setup = OnpremSetup(has_cipher=False)
    registration = await setup.service.create_server(OWNER, "home-lab")

    with pytest.raises(NotConfiguredError):
        await setup.connect(registration.registration_token, registration.server)


async def test_delete_server_with_attached_service_is_in_use() -> None:
    setup = OnpremSetup()
    server = (await setup.service.create_server(OWNER, "home-lab")).server
    await _attach_service(setup, server.target_id)

    with pytest.raises(OnpremServerInUseError):
        await setup.service.delete_server(OWNER, server.id)
    assert not server.is_deleted


async def test_delete_server_soft_deletes_server_and_target_and_schedules_cleanup() -> None:
    setup = OnpremSetup()
    server = (await setup.service.create_server(OWNER, "home-lab")).server
    server.record_gitops_commit("c-values")

    await setup.service.delete_server(OWNER, server.id)

    target = next(t for t in setup.targets.targets if t.id == server.target_id)
    assert server.is_deleted and target.is_deleted
    assert server.gitops_commit_sha is None
    assert server.next_check_at is not None
    with pytest.raises(OnpremServerNotFoundError):
        await setup.service.get_server(OWNER, server.id)


async def test_delete_other_owners_server_is_not_found() -> None:
    setup = OnpremSetup()
    server = (await setup.service.create_server(OWNER, "home-lab")).server

    with pytest.raises(OnpremServerNotFoundError):
        await setup.service.delete_server(OTHER_OWNER, server.id)


async def _connected_server_secret(setup: OnpremSetup) -> tuple[int, str]:
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)
    registration.server.mark_as_connected(datetime.now(UTC))
    return registration.server.target_id, secret


async def test_registry_credentials_scope_to_attached_services() -> None:
    setup = OnpremSetup()
    target_id, secret = await _connected_server_secret(setup)
    service = await _attach_service(setup, target_id)

    credentials = await setup.service.issue_registry_credentials(secret)

    assert credentials.service_ids == [service.id]
    assert credentials.password == "ecr-password"
    assert credentials.username == "AWS"
    session_name, repositories = setup.ecr.calls[0]
    assert session_name.startswith("iris-onprem-")
    assert repositories == [f"iris/services/{service.id}"]


async def test_registry_credentials_without_services_has_no_password() -> None:
    setup = OnpremSetup()
    _, secret = await _connected_server_secret(setup)

    credentials = await setup.service.issue_registry_credentials(secret)

    assert credentials.service_ids == []
    assert credentials.password is None
    assert credentials.registry == setup.ecr.registry
    assert setup.ecr.calls == []


async def test_registry_credentials_before_connected_is_not_connected_conflict() -> None:
    setup = OnpremSetup()
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)

    with pytest.raises(OnpremServerNotConnectedError) as error:
        await setup.service.issue_registry_credentials(secret)
    assert error.value.status_code == 409
    assert error.value.code == "ONPREM_SERVER_NOT_CONNECTED"


async def test_registry_credentials_with_wrong_secret_is_unauthorized() -> None:
    setup = OnpremSetup()
    await _connected_server_secret(setup)

    with pytest.raises(UnauthorizedError):
        await setup.service.issue_registry_credentials("wrong-secret")


async def test_registry_credentials_without_role_checks_secret_then_status_first() -> None:
    # 순서는 401(비밀) → 409(연결 전) → 503(설정 없음)이다.
    setup = OnpremSetup(has_ecr=False)
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)

    with pytest.raises(UnauthorizedError):
        await setup.service.issue_registry_credentials("wrong-secret")
    with pytest.raises(OnpremServerNotConnectedError):
        await setup.service.issue_registry_credentials(secret)
    registration.server.mark_as_connected(datetime.now(UTC))
    with pytest.raises(NotConfiguredError):
        await setup.service.issue_registry_credentials(secret)


async def test_registry_credentials_with_revoked_secret_is_unauthorized_even_without_role() -> None:
    setup = OnpremSetup(has_ecr=False)
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)
    await setup.service.reissue_registration_token(OWNER, registration.server.id)

    with pytest.raises(UnauthorizedError):
        await setup.service.issue_registry_credentials(secret)


async def test_registry_credentials_aws_failure_reports_service_count() -> None:
    setup = OnpremSetup()
    target_id, secret = await _connected_server_secret(setup)
    await _attach_service(setup, target_id)

    async def fail(session_name: str, repository_names: list[str]) -> None:
        raise ExternalError("aws request failed", operation="assume_role")

    setup.ecr.issue_pull_credential = fail  # type: ignore[assignment,method-assign]

    with pytest.raises(ExternalError) as error:
        await setup.service.issue_registry_credentials(secret)
    assert error.value.status_code == 502
    assert error.value.fields["service_count"] == 1
