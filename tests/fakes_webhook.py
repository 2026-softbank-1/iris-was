"""웹훅·배포 요청 테스트용 가짜 Repository."""

from itertools import count

from app.enums import ACTIVE_DEPLOYMENT_STATUSES, DeploymentStatus
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job
from app.models.service import Service


class FakeWebhookServiceRepository:
    def __init__(self, services: list[Service]) -> None:
        self.services = services

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
        request.status = DeploymentStatus.QUEUED
        self.requests.append(request)
        return request


class FakeJobRepository:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def save(self, job: Job) -> Job:
        self.jobs.append(job)
        return job
