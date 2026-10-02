from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import Environment, FailureCode, ReleaseStatus
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column, now_utc
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest


class Release(TimestampMixin, Base):
    """타깃 하나에 반영한 배포 결과. 같은 이미지를 타깃마다 한 건씩 남긴다.

    `deadline_at` 이 있으면 GitOps 커밋이 반영된 것이다. 요청의 `status` 는 여기서 바꾸지 않고
    호출하는 쪽이 `DeploymentStatusService` 로 옮긴다.
    """

    __tablename__ = "releases"
    __table_args__ = (
        # service + environment + target 의 lastKnownGood 조회 경로.
        Index("ix_releases_last_known_good", "service_id", "environment", "target_id", "status"),
        # 서비스·타깃마다 진행 중 release 는 하나다. 배포 직렬화를 이 index 로만 강제한다.
        Index(
            "uq_releases_service_id_target_id_in_flight",
            "service_id",
            "target_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'ROLLING_BACK')"),
        ),
    )

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), index=True
    )
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    environment: Mapped[Environment] = mapped_column(enum_column(Environment, "environment"))
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"))
    image_digest: Mapped[str] = mapped_column(String(80))
    gitops_commit_sha: Mapped[str | None] = mapped_column(String(64))
    # 실패한 release 를 되돌린 revert commit.
    revert_commit_sha: Mapped[str | None] = mapped_column(String(64))
    # Argo CD 가 주는 값을 원본 표기 그대로 저장한다 (Synced · Healthy 등).
    argo_sync_status: Mapped[str | None] = mapped_column(String(32))
    argo_health_status: Mapped[str | None] = mapped_column(String(32))
    previous_good_release_id: Mapped[int | None] = mapped_column(ForeignKey("releases.id"))
    status: Mapped[ReleaseStatus] = mapped_column(
        enum_column(ReleaseStatus, "release_status"), default=ReleaseStatus.PENDING
    )
    failure_code: Mapped[FailureCode | None] = mapped_column(
        enum_column(FailureCode, "failure_code")
    )
    # Argo CD 가 이 시각까지 반영·정상화하지 못하면 실패로 본다.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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

    def succeed(self) -> None:
        self._finish(ReleaseStatus.SUCCEEDED)

    def roll_back(self) -> None:
        self._finish(ReleaseStatus.ROLLED_BACK)

    def fail(self, failure_code: FailureCode) -> None:
        self.record_failure(failure_code)
        self._finish(ReleaseStatus.FAILED)

    def _finish(self, status: ReleaseStatus) -> None:
        self.status = status
        self.finished_at = now_utc()
