import hashlib
import hmac
import json
from typing import Any

import pytest

from app.core.exceptions import InvalidInputError, UnauthorizedError
from app.enums import DeploymentStatus, DeploymentTrigger, JobKind
from app.models.service import Service
from app.models.user import GithubInstallation
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
SHA = "a" * 40


def _service(service_id: int = 1, **overrides: Any) -> Service:
    values: dict[str, Any] = {
        "id": service_id,
        "name": f"svc-{service_id}",
        "source_repository_url": REPO_URL,
        "source_branch": "main",
        "root_directory": None,
        "is_auto_deploy": True,
        "is_deleted": False,
    }
    return Service(**(values | overrides))


def _push(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ref": "refs/heads/main",
        "after": SHA,
        "deleted": False,
        "head_commit": {"message": "fix: login"},
        "commits": [{"message": "fix: login", "modified": ["src/app.py"]}],
        "repository": {"html_url": REPO_URL.lower()},
    }
    return payload | overrides


def _signed(payload: dict[str, Any]) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


class Parts:
    def __init__(self, services: list[Service]) -> None:
        self.session = FakeSession()
        self.requests = FakeDeploymentRequestRepository()
        self.jobs = FakeJobRepository()
        self.builds = FakeBuildRepository()
        self.histories = FakeDeploymentStatusHistoryRepository()
        self.variables = FakeServiceVariableRepository()
        self.installations = FakeGithubInstallationRepository()
        self.service = WebhookService(
            self.session,  # type: ignore[arg-type]
            FakeWebhookServiceRepository(services),  # type: ignore[arg-type]
            self.installations,  # type: ignore[arg-type]
            DeploymentRequestService(
                self.requests,  # type: ignore[arg-type]
                self.jobs,  # type: ignore[arg-type]
                self.histories,  # type: ignore[arg-type]
                self.builds,  # type: ignore[arg-type]
                self.variables,  # type: ignore[arg-type]
            ),
            SECRET,
        )

    async def receive(self, event: str, payload: dict[str, Any], delivery_id: str = "d-1"):  # type: ignore[no-untyped-def]
        body, signature = _signed(payload)
        return await self.service.receive_github_event(
            event=event, delivery_id=delivery_id, signature=signature, body=body
        )


async def test_receive_with_invalid_signature_raises_unauthorized() -> None:
    parts = Parts([_service()])

    with pytest.raises(UnauthorizedError):
        await parts.service.receive_github_event(
            event="push", delivery_id="d", signature="sha256=bad", body=b"{}"
        )
    with pytest.raises(UnauthorizedError):
        await parts.service.receive_github_event(
            event="push", delivery_id="d", signature=None, body=b"{}"
        )
    assert parts.requests.requests == []


async def test_receive_with_non_json_body_raises_invalid_input() -> None:
    parts = Parts([])
    body = b"not json"
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    with pytest.raises(InvalidInputError):
        await parts.service.receive_github_event(
            event="push", delivery_id="d", signature=signature, body=body
        )


async def test_push_snapshots_current_variables() -> None:
    parts = Parts([_service()])
    await parts.variables.replace_all(1, {"A": "enc(1)"})

    await parts.receive("push", _push())

    assert parts.requests.requests[0].variables_snapshot == {"A": "enc(1)"}


async def test_push_creates_deployment_request_and_build_job() -> None:
    parts = Parts([_service()])

    receipt = await parts.receive("push", _push())

    assert receipt.is_handled is True
    assert receipt.deployment_request_ids == [1]
    request = parts.requests.requests[0]
    assert (request.trigger_type, request.source_sha, request.source_commit_message) == (
        DeploymentTrigger.PUSH,
        SHA,
        "fix: login",
    )
    assert request.requested_by is None
    job = parts.jobs.jobs[0]
    assert job.kind == JobKind.BUILD
    build = parts.builds.builds[0]
    assert build.deployment_request_id == request.id
    assert job.payload == {"build_id": build.id}
    first_history = parts.histories.histories[0]
    assert (first_history.from_status, first_history.to_status) == (
        None,
        DeploymentStatus.QUEUED,
    )
    assert parts.session.commit_count == 1


async def test_push_redelivery_does_not_create_duplicate_request() -> None:
    parts = Parts([_service()])
    await parts.receive("push", _push(), delivery_id="same")
    parts.requests.requests[0].status = DeploymentStatus.SUCCEEDED

    receipt = await parts.receive("push", _push(), delivery_id="same")

    assert receipt.is_handled is False
    assert len(parts.requests.requests) == 1


async def test_push_while_service_has_active_request_is_skipped() -> None:
    parts = Parts([_service()])
    await parts.receive("push", _push(), delivery_id="d-1")

    receipt = await parts.receive("push", _push(after="b" * 40), delivery_id="d-2")

    assert receipt.deployment_request_ids == []
    assert len(parts.requests.requests) == 1


async def test_push_creates_one_request_per_connected_service() -> None:
    parts = Parts([_service(1), _service(2)])

    receipt = await parts.receive("push", _push())

    assert receipt.deployment_request_ids == [1, 2]
    assert {r.idempotency_key for r in parts.requests.requests} == {
        "github-push:d-1:1",
        "github-push:d-1:2",
    }


@pytest.mark.parametrize(
    "service",
    [
        _service(source_branch="develop"),
        _service(is_auto_deploy=False),
        _service(is_deleted=True),
        _service(source_repository_url="https://github.com/other/repo"),
    ],
    ids=["other-branch", "auto-deploy-off", "deleted", "other-repository"],
)
async def test_push_does_not_deploy_unrelated_service(service: Service) -> None:
    parts = Parts([service])

    receipt = await parts.receive("push", _push())

    assert receipt.is_handled is False
    assert parts.requests.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        _push(ref="refs/tags/v1"),
        _push(deleted=True, after="0" * 40),
        _push(after="0" * 40),
    ],
    ids=["tag", "branch-deleted", "null-sha"],
)
async def test_push_that_is_not_a_branch_update_is_ignored(payload: dict[str, Any]) -> None:
    parts = Parts([_service()])

    receipt = await parts.receive("push", payload)

    assert receipt.is_handled is False
    assert parts.requests.requests == []


@pytest.mark.parametrize(
    ("modified", "expected"),
    [
        (["web/index.ts"], 0),
        (["api/main.py"], 1),
        (["api/sub/deep.py", "README.md"], 1),
        (["apiary/readme.md"], 0),
        ([], 1),
    ],
)
async def test_push_for_monorepo_service_checks_changed_paths(
    modified: list[str], expected: int
) -> None:
    parts = Parts([_service(root_directory="api")])
    payload = _push(commits=[{"message": "m", "modified": modified}] if modified else [])

    await parts.receive("push", payload)

    assert len(parts.requests.requests) == expected


async def test_unknown_event_is_acknowledged_without_side_effects() -> None:
    parts = Parts([_service()])

    receipt = await parts.receive("ping", {"zen": "Keep it logically awesome."})

    assert receipt.is_handled is False
    assert parts.session.commit_count == 0


async def test_malformed_push_payload_raises_invalid_input() -> None:
    parts = Parts([_service()])

    with pytest.raises(InvalidInputError):
        await parts.receive("push", {"ref": "refs/heads/main"})


def _installation_event(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 777, "account": {"login": "acme", "type": "Organization"}},
    }


async def test_installation_created_registers_installation() -> None:
    parts = Parts([])

    receipt = await parts.receive("installation", _installation_event("created"))

    assert receipt.is_handled is True
    saved = await parts.installations.find_by_installation_id(777)
    assert saved is not None
    assert (saved.account_login, saved.account_type) == ("acme", "Organization")


async def test_installation_created_again_updates_account() -> None:
    parts = Parts([])
    await parts.installations.save(
        GithubInstallation(installation_id=777, account_login="old", account_type="User")
    )

    await parts.receive("installation", _installation_event("created"))

    saved = await parts.installations.find_by_installation_id(777)
    assert saved is not None
    assert saved.account_login == "acme"
    assert len(parts.installations.installations) == 1


async def test_installation_deleted_unlinks_users_but_keeps_installation() -> None:
    parts = Parts([])
    saved = await parts.installations.save(
        GithubInstallation(installation_id=777, account_login="acme", account_type="User")
    )
    await parts.installations.replace_user_links(1, {saved.id})

    receipt = await parts.receive("installation", _installation_event("deleted"))

    assert receipt.is_handled is True
    assert await parts.installations.search_by_user_id(1) == []
    assert await parts.installations.find_by_installation_id(777) is not None


@pytest.mark.parametrize("action", ["suspend", "unsuspend", "new_permissions_accepted"])
async def test_installation_other_actions_are_ignored(action: str) -> None:
    parts = Parts([])

    receipt = await parts.receive("installation", _installation_event(action))

    assert receipt.is_handled is False
    assert parts.session.commit_count == 0
