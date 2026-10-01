from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import DeploymentStatus, FailureCode, ReleaseStatus
from app.models.base import Base, TimestampMixin
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest


class Release(TimestampMixin, Base):
    """GitOps 에 반영한 배포 1건. deadline_at 이 있으면 커밋이 main 에 올라간 것이다."""

    __tablename__ = "releases"
    __table_args__ = (
        # 서비스당 진행 중 release 는 하나다. 배포 직렬화를 이 index 로만 강제한다.
        Index(
            "uq_releases_service_id_in_flight",
            "service_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'ROLLING_BACK')"),
        ),
        Index("ix_releases_service_id_status_id", "service_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), unique=True
    )
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    image_digest: Mapped[str]
    gitops_commit_sha: Mapped[str | None]
    revert_commit_sha: Mapped[str | None]
    previous_good_release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id"))
    status: Mapped[ReleaseStatus] = mapped_column(server_default=ReleaseStatus.PENDING.value)
    failure_code: Mapped[FailureCode | None]
    deadline_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    deployment_request: Mapped[DeploymentRequest] = relationship(lazy="raise")
    build: Mapped[Build] = relationship(lazy="raise")

    @property
    def is_finished(self) -> bool:
        return self.status in (
            ReleaseStatus.SUCCEEDED,
            ReleaseStatus.FAILED,
            ReleaseStatus.ROLLED_BACK,
        )

    @property
    def target_commit_sha(self) -> str | None:
        """Argo CD 가 반영해야 하는 커밋. rollback 중이면 revert commit 이다."""
        if self.status == ReleaseStatus.ROLLING_BACK:
            return self.revert_commit_sha
        return self.gitops_commit_sha

    def record_commit(self, commit_sha: str) -> None:
        self.gitops_commit_sha = commit_sha

    def confirm_commit(self, deadline_at: datetime) -> None:
        self.deadline_at = deadline_at

    def record_revert(self, commit_sha: str) -> None:
        self.revert_commit_sha = commit_sha

    def start_rollback(self, deadline_at: datetime) -> None:
        self.deadline_at = deadline_at
        self.status = ReleaseStatus.ROLLING_BACK

    def record_failure(self, failure_code: FailureCode) -> None:
        self.failure_code = failure_code
        self.deployment_request.failure_code = failure_code

    def succeed(self) -> None:
        self._finish(ReleaseStatus.SUCCEEDED, DeploymentStatus.SUCCEEDED)

    def roll_back(self) -> None:
        self._finish(ReleaseStatus.ROLLED_BACK, DeploymentStatus.ROLLED_BACK)

    def fail(
        self,
        failure_code: FailureCode,
        request_status: DeploymentStatus = DeploymentStatus.FAILED,
    ) -> None:
        self.record_failure(failure_code)
        self._finish(ReleaseStatus.FAILED, request_status)

    def _finish(self, status: ReleaseStatus, request_status: DeploymentStatus) -> None:
        self.status = status
        self.finished_at = datetime.now(UTC)
        self.deployment_request.status = request_status
