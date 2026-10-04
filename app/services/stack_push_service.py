"""푸시가 스택 레포에 오면: 바뀐 앱만 스택 순서대로 다시 빌드·배포하고, 같은 커밋으로 레포를 다시
분석한다.

바뀐 앱 판단은 기존 웹훅 규칙(서비스 root_directory 아래 파일이 바뀌었는지)이다. 관리형 DB 는 소스가
없어 푸시로 다시 배포하지 않는다. 재분석 결과가 스택의 기준 분석과 다르면 Build Worker 가 스택에
pendingChanges 를 남긴다(`stack_changes`).
"""

import logging
from dataclasses import dataclass, field

from app.enums import DeploymentTrigger
from app.models.service import Service
from app.repositories.project_repository import ProjectRepository
from app.repositories.repository_analysis_repository import RepositoryAnalysisRepository
from app.repositories.service_stack_repository import ServiceStackRepository
from app.services.stack_service import StackService
from app.services.variable_validation import VariableValidationService

logger = logging.getLogger(__name__)


@dataclass
class StackPushResult:
    deployment_request_ids: list[int] = field(default_factory=list)
    repository_analysis_ids: list[int] = field(default_factory=list)


class StackPushService:
    def __init__(
        self,
        stack_service: StackService,
        stack_repository: ServiceStackRepository,
        repository_analysis_repository: RepositoryAnalysisRepository,
        project_repository: ProjectRepository,
        variable_validation_service: VariableValidationService,
    ) -> None:
        self._stack_service = stack_service
        self._stack_repository = stack_repository
        self._repository_analysis_repository = repository_analysis_repository
        self._project_repository = project_repository
        self._variable_validation_service = variable_validation_service

    async def handle_push(
        self,
        *,
        delivery_id: str,
        repository_url: str,
        branch: str,
        source_sha: str,
        commit_message: str | None,
        changed_services: list[Service],
    ) -> StackPushResult:
        """커밋하지 않는다. `changed_services` 는 스택에 속한, 경로가 바뀐 자동 배포 앱이다."""
        result = StackPushResult()
        by_stack: dict[int, list[Service]] = {}
        for service in changed_services:
            assert service.stack_id is not None
            by_stack.setdefault(service.stack_id, []).append(service)
        for stack_id, services in sorted(by_stack.items()):
            stack = await self._stack_repository.get_by_id(stack_id)
            blocked_ids: set[int] = set()
            for service in services:
                if not (await self._variable_validation_service.validate(service)).ok:
                    blocked_ids.add(service.id)
            blocked = frozenset(blocked_ids)

            async def source() -> tuple[str, str | None]:
                return source_sha, commit_message

            created = await self._stack_service.create_stack_deployment(
                stack,
                services,
                trigger_type=DeploymentTrigger.PUSH,
                # 같은 delivery 가 다시 와도(GitHub 재전송) 스택 배포가 중복되지 않는다.
                idempotency_key=f"github-push:{delivery_id}:stack:{stack.id}",
                requested_by=None,
                source=source,
                is_strict=False,
                blocked_service_ids=blocked,
            )
            result.deployment_request_ids.extend(
                request.id
                for service_id, request in sorted(created.requests.items())
                if service_id not in blocked
            )
        for stack in await self._stack_repository.search_by_repository(repository_url, branch):
            project = await self._project_repository.find_by_id(stack.project_id)
            if project is None:
                continue
            analysis = await self._repository_analysis_repository.add_stack_analysis_if_absent(
                stack_id=stack.id,
                project_id=stack.project_id,
                user_id=project.owner_id,
                source_repository_url=stack.source_repository_url,
                github_installation_id=stack.github_installation_id,
                source_branch=stack.source_branch,
                source_sha=source_sha,
                root_directory=stack.root_directory,
            )
            if analysis is not None:
                result.repository_analysis_ids.append(analysis.id)
                logger.info(
                    "stack reanalysis requested",
                    extra={
                        "action": "handle_push",
                        "stack_id": stack.id,
                        "repository_analysis_id": analysis.id,
                    },
                )
        return result
