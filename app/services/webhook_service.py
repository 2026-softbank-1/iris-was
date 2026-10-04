import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidInputError, UnauthorizedError
from app.core.security import verify_github_signature
from app.enums import DeploymentTrigger, FailureCode
from app.models.service import Service
from app.models.user import GithubInstallation
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.service_repository import ServiceRepository
from app.schemas.webhook import (
    GithubCommit,
    GithubInstallationEvent,
    GithubPushEvent,
    WebhookReceiptResponse,
)
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.stack_progress import fail_before_start
from app.services.stack_push_service import StackPushService
from app.services.variable_validation import VariableValidationService

logger = logging.getLogger(__name__)

_BRANCH_REF_PREFIX = "refs/heads/"
# 브랜치를 지우는 push 의 after 값.
_NULL_SHA = "0" * 40
# GitHub 는 push 의 commits 를 20 개까지만 싣는다. 이를 넘으면 바뀐 파일 목록을 믿을 수 없다.
_PUSH_COMMITS_LIMIT = 20


class WebhookService:
    """GitHub 웹훅을 검증하고 이벤트별로 처리한다. 모르는 이벤트는 받기만 하고 넘긴다."""

    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        installation_repository: GithubInstallationRepository,
        deployment_request_service: DeploymentRequestService,
        webhook_secret: str,
        *,
        stack_push_service: StackPushService | None = None,
        variable_validation_service: VariableValidationService | None = None,
    ) -> None:
        # 스택 레포 push(스택 순서 배포·재분석)와 배포 전 환경변수 검증. 없으면 이전 동작이다.
        self._stack_push_service = stack_push_service
        self._variable_validation_service = variable_validation_service
        self._session = session
        self._service_repository = service_repository
        self._installation_repository = installation_repository
        self._deployment_request_service = deployment_request_service
        self._webhook_secret = webhook_secret

    async def receive_github_event(
        self, *, event: str, delivery_id: str, signature: str | None, body: bytes
    ) -> WebhookReceiptResponse:
        if not verify_github_signature(self._webhook_secret, body, signature):
            raise UnauthorizedError("invalid webhook signature", delivery_id=delivery_id)

        payload = _parse_json(body)
        logger.info(
            "github webhook received",
            extra={"action": "receive_github_event", "event": event, "delivery_id": delivery_id},
        )
        try:
            if event == "push":
                return await self._handle_push(delivery_id, GithubPushEvent.model_validate(payload))
            if event == "installation":
                return await self._handle_installation(
                    GithubInstallationEvent.model_validate(payload)
                )
        except ValidationError as exc:
            raise InvalidInputError("unexpected webhook payload", event=event) from exc
        return WebhookReceiptResponse(is_handled=False)

    async def _handle_push(self, delivery_id: str, push: GithubPushEvent) -> WebhookReceiptResponse:
        if not push.ref.startswith(_BRANCH_REF_PREFIX) or push.deleted or push.after == _NULL_SHA:
            return WebhookReceiptResponse(is_handled=False)

        branch = push.ref.removeprefix(_BRANCH_REF_PREFIX)
        services = await self._service_repository.search_auto_deploy_by_repository_url_and_branch(
            push.repository.html_url, branch
        )
        message = push.head_commit.message if push.head_commit is not None else None
        changed_paths = _collect_changed_paths(push.commits)

        deployment_request_ids: list[int] = []
        analysis_ids: list[int] = []
        changed = [
            service
            for service in services
            if _is_service_changed(service, changed_paths, len(push.commits))
        ]
        stack_push = self._stack_push_service
        for service in changed:
            if stack_push is not None and service.stack_id is not None:
                continue
            is_blocked = (
                self._variable_validation_service is not None
                and not (await self._variable_validation_service.validate(service)).ok
            )
            request = await self._deployment_request_service.create_deployment_request(
                service,
                source_sha=push.after,
                source_commit_message=message,
                trigger_type=DeploymentTrigger.PUSH,
                # 같은 delivery 가 다시 와도(GitHub 재전송) 같은 서비스에 요청이 중복되지 않는다.
                idempotency_key=f"github-push:{delivery_id}:{service.id}",
                is_started=not is_blocked,
            )
            if request is None:
                continue
            if is_blocked:
                # 자동 배포는 사용자에게 422 를 줄 수 없어 실패한 요청으로 남긴다(빌드하지 않는다).
                await fail_before_start(
                    self._session,
                    DeploymentStatusService.create(self._session),
                    request.id,
                    FailureCode.VARIABLES_INVALID,
                )
                continue
            deployment_request_ids.append(request.id)
        if stack_push is not None:
            stacked = await stack_push.handle_push(
                delivery_id=delivery_id,
                repository_url=push.repository.html_url,
                branch=branch,
                source_sha=push.after,
                commit_message=message,
                changed_services=[s for s in changed if s.stack_id is not None],
            )
            deployment_request_ids.extend(stacked.deployment_request_ids)
            analysis_ids.extend(stacked.repository_analysis_ids)

        await self._session.commit()
        for deployment_request_id in deployment_request_ids:
            logger.info(
                "push deployment requested",
                extra={
                    "action": "handle_push",
                    "deployment_request_id": deployment_request_id,
                    "delivery_id": delivery_id,
                },
            )
        return WebhookReceiptResponse(
            is_handled=bool(deployment_request_ids or analysis_ids),
            deployment_request_ids=deployment_request_ids,
            repository_analysis_ids=analysis_ids,
        )

    async def _handle_installation(self, event: GithubInstallationEvent) -> WebhookReceiptResponse:
        detail = event.installation
        installation = await self._installation_repository.find_by_installation_id(detail.id)

        if event.action == "created":
            if installation is None:
                installation = GithubInstallation(installation_id=detail.id)
            installation.account_login = detail.account.login
            installation.account_type = detail.account.type
            await self._installation_repository.save(installation)
        elif event.action == "deleted":
            if installation is None:
                return WebhookReceiptResponse(is_handled=False)
            await self._installation_repository.delete_user_links_by_github_installation_id(
                installation.id
            )
        else:
            return WebhookReceiptResponse(is_handled=False)

        await self._session.commit()
        logger.info(
            "github installation synced",
            extra={"action": "sync_installation", "installation_id": detail.id},
        )
        return WebhookReceiptResponse(is_handled=True)


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError as exc:
        raise InvalidInputError("webhook body is not json") from exc


def _collect_changed_paths(commits: list[GithubCommit]) -> set[str]:
    return {
        path for commit in commits for path in (*commit.added, *commit.modified, *commit.removed)
    }


def _is_service_changed(service: Service, changed_paths: set[str], commit_count: int) -> bool:
    """모노레포의 서비스는 자기 디렉터리가 바뀐 push 에만 배포한다. 판단할 수 없으면 배포한다."""
    root = (service.root_directory or "").strip("/")
    if not root or not changed_paths or commit_count >= _PUSH_COMMITS_LIMIT:
        return True
    return any(path == root or path.startswith(f"{root}/") for path in changed_paths)
