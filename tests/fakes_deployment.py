"""배포 요청 생성·전이·조회 테스트가 함께 쓰는 조립 도우미."""

from unittest.mock import AsyncMock

from app.clients.source_repository_client import CommitInfo
from app.enums import DeploymentStatus, DeploymentTrigger, Environment, ReleaseStatus
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.services.deployment_history_service import DeploymentHistoryService
from app.services.deployment_log_service import DeploymentLogService
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSession,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository, FakeTargetRepository
from tests.fakes_variable import FakeServiceVariableRepository
from tests.fakes_webhook import (
    FakeBuildRepository,
    FakeDeploymentRequestRepository,
    FakeDeploymentStatusHistoryRepository,
    FakeJobRepository,
    FakeReleaseRepository,
)

OWNER = 1
HEAD_SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
FULL_NAME = "iris-org/web"


class DeploymentSetup:
    """사용자 1(OWNER)의 서비스 하나와, 그 서비스를 다루는 서비스 객체들을 인메모리로 조립한다."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.installations = FakeGithubInstallationRepository()
        self.requests = FakeDeploymentRequestRepository()
        self.jobs = FakeJobRepository()
        self.builds = FakeBuildRepository()
        self.releases = FakeReleaseRepository()
        self.targets = FakeTargetRepository()
        self.histories = FakeDeploymentStatusHistoryRepository()
        self.variables = FakeServiceVariableRepository()
        self.github = FakeSourceRepositoryClient({22: [make_repository(FULL_NAME)]})
        self.github.heads[(FULL_NAME, "main")] = CommitInfo(HEAD_SHA, "feat: add login")
        # 외부 백엔드(Loki·CloudWatch)는 호출 인자만 확인하는 가짜다.
        self.observability = AsyncMock()
        self.build_log_reader = AsyncMock()
        self.service: Service

    async def build(self) -> "DeploymentSetup":
        installation = await self.installations.save(make_installation(5, 22, "iris-org"))
        await self.installations.replace_user_links(OWNER, {installation.id})
        project = await self.projects.save(Project(name="p", owner_id=OWNER))
        self.service = await self.services.save(
            Service(
                project_id=project.id,
                name="web",
                source_repository_url=f"https://github.com/{FULL_NAME}",
                github_installation_id=installation.id,
                source_branch="main",
                root_directory=None,
                is_auto_deploy=True,
            )
        )
        return self

    def deployment_request_service(self) -> DeploymentRequestService:
        return DeploymentRequestService(
            self.requests,  # type: ignore[arg-type]
            self.jobs,  # type: ignore[arg-type]
            self.histories,  # type: ignore[arg-type]
            self.builds,  # type: ignore[arg-type]
            self.variables,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
        )

    def manual_service(self) -> ManualDeploymentService:
        return ManualDeploymentService(
            self.session,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.requests,  # type: ignore[arg-type]
            self.builds,  # type: ignore[arg-type]
            self.deployment_request_service(),
            SourceRepositoryService(self.installations, self.github),  # type: ignore[arg-type]
        )

    def history_service(self) -> DeploymentHistoryService:
        return DeploymentHistoryService(
            self.services,  # type: ignore[arg-type]
            self.requests,  # type: ignore[arg-type]
            self.histories,  # type: ignore[arg-type]
            self.builds,  # type: ignore[arg-type]
            self.releases,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
        )

    def log_service(
        self, *, has_build_log_reader: bool = True, build_log_group: str | None = "/aws/codebuild/x"
    ) -> DeploymentLogService:
        return DeploymentLogService(
            self.history_service(),
            self.requests,  # type: ignore[arg-type]
            self.builds,  # type: ignore[arg-type]
            self.observability,
            self.build_log_reader if has_build_log_reader else None,
            build_log_group,
        )

    async def create_succeeded_request(
        self, trigger_type: DeploymentTrigger = DeploymentTrigger.MANUAL
    ) -> DeploymentRequest:
        """QUEUED → BUILDING → DEPLOYING → SUCCEEDED 를 실제 전이로 거친 요청."""
        request = await self.manual_service().create_deployment_request(
            OWNER, self.service.id, trigger_type=trigger_type
        )
        status_service = self.status_service()
        await status_service.transition_status(request.id, DeploymentStatus.BUILDING)
        await status_service.transition_status(request.id, DeploymentStatus.DEPLOYING)
        await status_service.transition_status(request.id, DeploymentStatus.SUCCEEDED)
        return request

    def add_release(self, request: DeploymentRequest, target_id: int = 1) -> Release:
        release = Release(
            deployment_request_id=request.id,
            build_id=1,
            service_id=request.service_id,
            environment=Environment.PROD,
            target_id=target_id,
            image_digest="sha256:" + "a" * 64,
            status=ReleaseStatus.SUCCEEDED,
        )
        release.id = len(self.releases.releases) + 1
        self.releases.releases.append(release)
        return release

    def status_service(self) -> DeploymentStatusService:
        return DeploymentStatusService(
            self.requests,  # type: ignore[arg-type]
            self.histories,  # type: ignore[arg-type]
        )

    def finish_active_requests(self, status: DeploymentStatus = DeploymentStatus.SUCCEEDED) -> None:
        """진행 중인 요청을 끝내 새 요청을 받을 수 있게 한다(상태만 바꾼다)."""
        for request in self.requests.requests:
            request.status = status
