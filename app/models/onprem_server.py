from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import OnpremServerFailureCode, OnpremServerStatus
from app.models.base import Base, BigIntPk, SoftDeleteMixin, TimestampMixin, enum_column


class OnpremServer(TimestampMixin, SoftDeleteMixin, Base):
    """사용자가 배포 대상으로 직접 붙인 온프레미스 서버 1대. 서버마다 전용 타깃이 하나 있다.

    Deploy Worker 가 `next_check_at` 이 된 행을 lease(`locked_by`·`locked_until`)로 선점해
    GitOps 반영·연결 확인·삭제 정리를 한다. connect 를 다시 받으면 `connect_generation` 이 올라,
    그 전에 선점한 Worker 의 결과는 버려진다.
    """

    __tablename__ = "onprem_servers"
    __table_args__ = (
        Index(
            "uq_onprem_servers_owner_id_name",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("NOT is_deleted"),
        ),
        # Worker 가 할 일이 있는 서버를 찾는 경로. 할 일이 없는 행은 next_check_at 이 비어 있다.
        Index(
            "ix_onprem_servers_next_check_at",
            "next_check_at",
            postgresql_where=text("next_check_at IS NOT NULL"),
        ),
    )

    id: Mapped[BigIntPk]
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(63))
    # 모든 이름(타깃·Argo cluster·Tailscale hostname·host)의 기준. 비밀이 아니다.
    server_key: Mapped[str] = mapped_column(String(8), unique=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), unique=True)
    status: Mapped[OnpremServerStatus] = mapped_column(
        enum_column(OnpremServerStatus, "onprem_server_status"),
        default=OnpremServerStatus.PENDING,
    )
    failure_code: Mapped[OnpremServerFailureCode | None] = mapped_column(
        enum_column(OnpremServerFailureCode, "onprem_server_failure_code")
    )
    # 등록 토큰의 SHA-256(hex). 평문은 등록·재발급 응답에서만 보인다.
    registration_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    registration_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tailnet_fqdn: Mapped[str | None] = mapped_column(String(255))
    # K3s API server CA(PEM). 공개값이다.
    api_ca_cert: Mapped[str | None] = mapped_column(Text)
    # 만료 없는 ServiceAccount 토큰의 Fernet 암호문. 평문은 Worker 가 봉인할 때만 메모리에 있다.
    encrypted_service_account_token: Mapped[str | None] = mapped_column(Text)
    # 서버의 Sealed Secrets controller 공개 인증서(PEM). 이 서버로 가는 서비스 변수를 봉인한다.
    sealed_secrets_cert: Mapped[str | None] = mapped_column(Text)
    # 서버 비밀의 SHA-256(hex). 서버가 ECR 자격증명을 받을 때 Bearer 로 보낸다.
    server_secret_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    # connect 를 받을 때마다 +1. Worker 는 선점할 때의 값과 같을 때만 결과를 쓴다.
    connect_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # 서버 디렉터리를 바꾼(또는 지운) GitOps 커밋. 외부에 쓰기 전에 먼저 기록한다.
    gitops_commit_sha: Mapped[str | None] = mapped_column(String(40))
    # 지금 커밋을 위해 실패한 횟수. 한도를 넘으면 FAILED(GITOPS_COMMIT_FAILED)다.
    gitops_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # 커밋이 main 에 반영된 뒤 정한다. 이 시각까지 probe 가 정상이 아니면 FAILED 다.
    connect_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Worker 가 이 시각 이후에 이 행을 처리한다. 할 일이 없으면 비어 있다.
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    def is_registration_expired(self, now: datetime) -> bool:
        return self.registration_expires_at <= now

    def reissue_registration_token(self, token_hash: str, expires_at: datetime) -> None:
        """이전 토큰과 서버 비밀은 무효가 되고 등록을 처음부터 다시 한다."""
        self.registration_token_hash = token_hash
        self.registration_expires_at = expires_at
        self.status = OnpremServerStatus.PENDING
        self.failure_code = None
        self.server_secret_hash = None
        self.connect_deadline_at = None
        self.next_check_at = None

    def start_registering(
        self,
        *,
        tailnet_fqdn: str,
        api_ca_cert: str,
        encrypted_service_account_token: str,
        sealed_secrets_cert: str,
        server_secret_hash: str,
        now: datetime,
    ) -> None:
        """서버가 보낸 접속 정보로 덮어쓰고 Worker 가 처음부터 다시 반영하게 한다."""
        self.tailnet_fqdn = tailnet_fqdn
        self.api_ca_cert = api_ca_cert
        self.encrypted_service_account_token = encrypted_service_account_token
        self.sealed_secrets_cert = sealed_secrets_cert
        self.server_secret_hash = server_secret_hash
        self.status = OnpremServerStatus.REGISTERING
        self.failure_code = None
        self.connect_generation += 1
        self.gitops_commit_sha = None
        self.gitops_attempts = 0
        self.connect_deadline_at = None
        self.last_error = None
        self.next_check_at = now

    def record_gitops_commit(self, commit_sha: str) -> None:
        self.gitops_commit_sha = commit_sha

    def confirm_gitops_commit(self, deadline_at: datetime) -> None:
        self.connect_deadline_at = deadline_at
        self.gitops_attempts = 0
        self.last_error = None

    def postpone_connect_check(self, check_at: datetime, deadline_at: datetime) -> None:
        """연결을 확인할 수 없어 다음으로 미룬다. 확인 기한도 함께 미룬다."""
        self.next_check_at = check_at
        self.connect_deadline_at = deadline_at

    def record_gitops_failure(self, error: str, retry_at: datetime) -> None:
        self.gitops_attempts += 1
        self.last_error = error
        self.next_check_at = retry_at

    def mark_as_connected(self, now: datetime) -> None:
        self.status = OnpremServerStatus.CONNECTED
        self.connected_at = now
        self.next_check_at = None

    def fail(self, failure_code: OnpremServerFailureCode, error: str | None = None) -> None:
        self.status = OnpremServerStatus.FAILED
        self.failure_code = failure_code
        self.last_error = error
        self.next_check_at = None

    def stop_checks(self, error: str) -> None:
        """Worker 가 더 처리하지 않는다. 운영자가 보도록 마지막 오류를 남긴다."""
        self.last_error = error
        self.next_check_at = None

    def schedule_check(self, at: datetime | None) -> None:
        self.next_check_at = at

    def release_lease(self) -> None:
        self.locked_by = None
        self.locked_until = None

    def remove(self, now: datetime) -> None:
        """소프트 삭제하고 Worker 가 GitOps 의 서버 디렉터리를 지우게 한다."""
        self.mark_as_deleted()
        self.gitops_commit_sha = None
        self.gitops_attempts = 0
        self.last_error = None
        self.next_check_at = now
