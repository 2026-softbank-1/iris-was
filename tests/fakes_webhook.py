"""웹훅·배포 요청 테스트용 가짜 Repository."""

from itertools import count

from app.core.exceptions import DeploymentRequestNotFoundError
from app.enums import (
    ACTIVE_DEPLOYMENT_STATUSES,
    BuildStatus,
    DeploymentStatus,
    DeploymentStrategy,
    Environment,
    TargetKind,
)
from app.models.base import now_utc
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.release import Release
from app.models.service import Service
from app.repositories.service_repository import DeploymentSettings


class FakeWebhookServiceRepository:
    def __init__(self, services: list[Service]) -> None:
        self.services = services

    async def get_deployment_settings_for_update(self, service_id: int) -> DeploymentSettings:
        service = next(service for service in self.services if service.id == service_id)
        return DeploymentSettings(
            service.scaling_config,
            service.deployment_strategy or DeploymentStrategy.ROLLING,
            TargetKind.AWS,
        )

    async def search_auto_deploy_by_repository_url_and_branch(
        self, repository_url: str, branch: str
    ) -> list[Service]:
        return [
            s
            for s in self.services
            if s.source_repository_url.lower() == repository_url.lower()
            and s.source_branch == branch
            and s.is_auto_deploy
            and not s.is_deleted
        ]


class FakeDeploymentRequestRepository:
    def __init__(self) -> None:
        self.requests: list[DeploymentRequest] = []
        self._ids = count(1)

    async def find_latest_by_source_sha(
        self, service_id: int, source_sha: str
    ) -> DeploymentRequest | None:
        rows = [
            r for r in self.requests if r.service_id == service_id and r.source_sha == source_sha
        ]
        return max(rows, key=lambda r: r.id) if rows else None

    async def find_by_idempotency_key(self, idempotency_key: str) -> DeploymentRequest | None:
        return next((r for r in self.requests if r.idempotency_key == idempotency_key), None)

    async def find_by_id_and_service_id(
        self, deployment_request_id: int, service_id: int
    ) -> DeploymentRequest | None:
        return next(
            (
                r
                for r in self.requests
                if r.id == deployment_request_id and r.service_id == service_id
            ),
            None,
        )

    async def find_latest_succeeded_by_service_id(
        self, service_id: int
    ) -> DeploymentRequest | None:
        succeeded = [
            r
            for r in self.requests
            if r.service_id == service_id and r.status == DeploymentStatus.SUCCEEDED
        ]
        return max(succeeded, key=lambda r: (r.created_at, r.id), default=None)

    async def find_first_succeeded_after(
        self, service_id: int, environment: Environment, deployment_request_id: int
    ) -> DeploymentRequest | None:
        newer = [
            r
            for r in self.requests
            if r.service_id == service_id
            and r.environment == environment
            and r.status == DeploymentStatus.SUCCEEDED
            and r.id > deployment_request_id
        ]
        return min(newer, key=lambda r: r.id, default=None)

    async def get_by_id_for_update(self, deployment_request_id: int) -> DeploymentRequest:
        request = next((r for r in self.requests if r.id == deployment_request_id), None)
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found", deployment_request_id=deployment_request_id
            )
        return request

    async def search_by_service_id(
        self, service_id: int, page: int, size: int
    ) -> list[DeploymentRequest]:
        found = sorted(
            (r for r in self.requests if r.service_id == service_id),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )
        return found[page * size : (page + 1) * size]

    async def count_by_service_id(self, service_id: int) -> int:
        return sum(1 for r in self.requests if r.service_id == service_id)

    async def search_latest_by_service_ids(
        self, service_ids: list[int]
    ) -> dict[int, DeploymentRequest]:
        return {
            r.service_id: r
            for r in sorted(self.requests, key=lambda r: r.id)
            if r.service_id in service_ids
        }

    async def add_if_absent(self, request: DeploymentRequest) -> DeploymentRequest | None:
        for existing in self.requests:
            if existing.idempotency_key == request.idempotency_key:
                return None
            if (
                existing.service_id == request.service_id
                and existing.environment == request.environment
                and existing.status in ACTIVE_DEPLOYMENT_STATUSES
            ):
                return None
        request.id = next(self._ids)
        while any(r.id == request.id for r in self.requests):
            request.id = next(self._ids)
        request.status = DeploymentStatus.QUEUED
        request.created_at = request.updated_at = now_utc()
        self.requests.append(request)
        return request


class FakeDeploymentStatusHistoryRepository:
    def __init__(self) -> None:
        self.histories: list[DeploymentStatusHistory] = []
        self._ids = count(1)

    async def add(self, history: DeploymentStatusHistory) -> DeploymentStatusHistory:
        history.id = next(self._ids)
        if history.created_at is None:
            history.created_at = now_utc()
        self.histories.append(history)
        return history

    async def search_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> list[DeploymentStatusHistory]:
        return sorted(
            (h for h in self.histories if h.deployment_request_id == deployment_request_id),
            key=lambda h: (h.created_at, h.id),
        )


class FakeBuildRepository:
    def __init__(self) -> None:
        self.builds: list[Build] = []

    async def find_by_deployment_request_id(self, deployment_request_id: int) -> Build | None:
        return next(
            (b for b in self.builds if b.deployment_request_id == deployment_request_id), None
        )

    async def add(self, build: Build) -> Build:
        build.id = len(self.builds) + 1
        # 컬럼 기본값은 INSERT 때 채워지므로 인메모리 도우미가 같게 맞춘다.
        if build.status is None:
            build.status = BuildStatus.PENDING
        self.builds.append(build)
        return build


class FakeReleaseRepository:
    def __init__(self) -> None:
        self.releases: list[Release] = []

    async def search_by_deployment_request_id(self, deployment_request_id: int) -> list[Release]:
        return sorted(
            (r for r in self.releases if r.deployment_request_id == deployment_request_id),
            key=lambda r: r.id,
        )


class FakeJobRepository:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def save(self, job: Job) -> Job:
        self.jobs.append(job)
        return job
