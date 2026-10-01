from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import Builder, BuildStatus, DeploymentStatus, FailureCode
from app.models.base import Base, TimestampMixin
from app.models.deployment_request import DeploymentRequest

_REQUEST_STATUS_BY_BUILD_STATUS = {
    BuildStatus.PENDING: DeploymentStatus.QUEUED,
    BuildStatus.SNAPSHOTTING: DeploymentStatus.INITIALIZING,
    BuildStatus.BUILDING: DeploymentStatus.BUILDING,
    BuildStatus.SUCCEEDED: DeploymentStatus.DEPLOYING,
    BuildStatus.FAILED: DeploymentStatus.FAILED,
    BuildStatus.CANCELLED: DeploymentStatus.SUPERSEDED,
}


class Build(TimestampMixin, Base):
    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), unique=True
    )
    status: Mapped[BuildStatus] = mapped_column(server_default=BuildStatus.PENDING.value)
    builder: Mapped[Builder | None]
    source_sha: Mapped[str | None]
    codebuild_build_id: Mapped[str | None]
    # CodeBuild 를 새로 시작할 때마다 +1. StartBuild idempotencyToken 에 넣는다.
    attempt: Mapped[int] = mapped_column(server_default="1")
    image_repository: Mapped[str | None]
    image_tag: Mapped[str | None]
    image_digest: Mapped[str | None]
    # iris.json 의 deploy.* 를 그대로 담는다. Deploy Worker 가 읽는다.
    deploy_config: Mapped[dict[str, Any] | None]
    failure_code: Mapped[FailureCode | None]
    log_url: Mapped[str | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    deployment_request: Mapped[DeploymentRequest] = relationship(lazy="raise")

    @property
    def is_finished(self) -> bool:
        return self.status in (BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED)

    def start_snapshot(self, source_sha: str) -> None:
        self.source_sha = source_sha
        self.deployment_request.source_sha = source_sha
        if self.started_at is None:
            self.started_at = datetime.now(UTC)
        self._transition(BuildStatus.SNAPSHOTTING)

    def start_codebuild(
        self,
        codebuild_build_id: str,
        builder: Builder,
        image_repository: str,
        image_tag: str,
        deploy_config: dict[str, Any],
    ) -> None:
        self.codebuild_build_id = codebuild_build_id
        self.builder = builder
        self.image_repository = image_repository
        self.image_tag = image_tag
        self.deploy_config = deploy_config
        self._transition(BuildStatus.BUILDING)

    def reset_codebuild(self) -> None:
        """CodeBuild 인프라 오류로 다시 빌드해야 할 때. 다음 시도는 스냅샷부터 새로 한다."""
        self.codebuild_build_id = None
        self.attempt += 1

    def succeed(self, image_digest: str) -> None:
        self.image_digest = image_digest
        self._finish(BuildStatus.SUCCEEDED)

    def fail(self, failure_code: FailureCode) -> None:
        self.failure_code = failure_code
        self.deployment_request.failure_code = failure_code
        self._finish(BuildStatus.FAILED)

    def cancel(self) -> None:
        self._finish(BuildStatus.CANCELLED)

    def _finish(self, status: BuildStatus) -> None:
        self.finished_at = datetime.now(UTC)
        self._transition(status)

    def _transition(self, status: BuildStatus) -> None:
        self.status = status
        self.deployment_request.status = _REQUEST_STATUS_BY_BUILD_STATUS[status]
