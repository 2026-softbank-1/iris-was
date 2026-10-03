from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import Builder, BuildStatus, FailureCode
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column, now_utc
from app.models.deployment_request import DeploymentRequest


class Build(TimestampMixin, Base):
    """배포 요청 1건당 한 번 하는 이미지 빌드. 결과는 image digest 로 남긴다.

    요청의 `status` 는 여기서 바꾸지 않는다. 호출하는 쪽이 `DeploymentStatusService` 로 옮긴다.
    """

    __tablename__ = "builds"

    id: Mapped[BigIntPk]
    deployment_request_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_requests.id"), unique=True
    )
    status: Mapped[BuildStatus] = mapped_column(
        enum_column(BuildStatus, "build_status"), default=BuildStatus.PENDING
    )
    # 빌더는 Worker 가 소스를 보고 확정한 뒤에 채운다.
    builder: Mapped[Builder | None] = mapped_column(enum_column(Builder, "builder"))
    source_sha: Mapped[str | None] = mapped_column(String(64))
    codebuild_build_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # CodeBuild 를 새로 시작할 때마다 +1. StartBuild idempotencyToken 에 넣는다.
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"), default=1)
    image_repository: Mapped[str | None] = mapped_column(String(500))
    # 빌드가 push 한 불변 태그. 배포 기준은 digest 이고 태그는 이미지를 찾는 데만 쓴다.
    image_tag: Mapped[str | None] = mapped_column(String(128))
    # `sha256:...` 값만 담는다.
    image_digest: Mapped[str | None] = mapped_column(String(80))
    # 서비스 레포 설정(iris.json)의 deploy.* 를 그대로 담는다. Deploy Worker 가 읽는다.
    deploy_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_code: Mapped[FailureCode | None] = mapped_column(
        enum_column(FailureCode, "failure_code")
    )
    log_url: Mapped[str | None] = mapped_column(Text)
    # 빌드가 실패했을 때 Build Worker 가 CloudWatch 에서 읽어 둔 로그 끝부분(비밀 패턴은 가렸다).
    # `{"entries": [{"timestamp", "message"}], "is_truncated"}`. AI 진단이 읽는다.
    log_tail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deployment_request: Mapped[DeploymentRequest] = relationship(lazy="raise")

    @classmethod
    def copy_succeeded(cls, source: "Build", deployment_request_id: int) -> "Build":
        """빌드 없이 원본의 이미지를 쓰는 요청(롤백·재시작)의 빌드. 이미지를 그대로 가리킨다."""
        now = now_utc()
        return cls(
            deployment_request_id=deployment_request_id,
            status=BuildStatus.SUCCEEDED,
            builder=source.builder,
            source_sha=source.source_sha,
            image_repository=source.image_repository,
            image_tag=source.image_tag,
            image_digest=source.image_digest,
            deploy_config=source.deploy_config,
            log_url=source.log_url,
            started_at=now,
            finished_at=now,
        )

    @property
    def is_finished(self) -> bool:
        return self.status in (BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED)

    def start_snapshot(self, source_sha: str) -> None:
        self.source_sha = source_sha
        if self.started_at is None:
            self.started_at = now_utc()
        self.status = BuildStatus.SNAPSHOTTING

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
        self.status = BuildStatus.BUILDING

    def record_log_tail(self, log_tail: dict[str, Any]) -> None:
        self.log_tail = log_tail

    def reset_codebuild(self) -> None:
        """CodeBuild 인프라 오류로 다시 빌드해야 할 때. 다음 시도는 스냅샷부터 새로 한다."""
        self.codebuild_build_id = None
        self.attempt += 1

    def succeed(self, image_digest: str) -> None:
        self.image_digest = image_digest
        self._finish(BuildStatus.SUCCEEDED)

    def fail(self, failure_code: FailureCode) -> None:
        self.failure_code = failure_code
        self._finish(BuildStatus.FAILED)

    def cancel(self) -> None:
        self._finish(BuildStatus.CANCELLED)

    def _finish(self, status: BuildStatus) -> None:
        self.finished_at = now_utc()
        self.status = status
