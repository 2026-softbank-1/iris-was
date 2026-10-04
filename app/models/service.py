from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import Builder, DatabaseEngine, DeploymentStrategy, ServiceKind
from app.models.base import Base, BigIntPk, SoftDeleteMixin, TimestampMixin, enum_column


class Service(TimestampMixin, SoftDeleteMixin, Base):
    """사용자가 배포하는 앱 하나. 소스 저장소 하나와 연결된다."""

    __tablename__ = "services"
    __table_args__ = (
        Index(
            "uq_services_project_id_name",
            "project_id",
            "name",
            unique=True,
            postgresql_where=text("NOT is_deleted"),
        ),
        # 한 스택 안에서 분석기 unit·dependency id 하나에는 서비스 하나다(증분 apply 의 매칭 기준).
        Index(
            "uq_services_stack_id_stack_unit_id",
            "stack_id",
            "stack_unit_id",
            unique=True,
            postgresql_where=text("stack_id IS NOT NULL AND NOT is_deleted"),
        ),
    )

    id: Mapped[BigIntPk]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    source_repository_url: Mapped[str] = mapped_column(String(500))
    # 관리형 DB(kind=DATABASE)는 소스가 없어 None 이다(저장소 주소·브랜치는 빈 문자열).
    github_installation_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_installations.id"), index=True
    )
    source_branch: Mapped[str] = mapped_column(String(255))
    # 저장소 안의 서비스 위치. None 이면 저장소 루트다.
    root_directory: Mapped[str | None] = mapped_column(String(255))
    is_auto_deploy: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    # 빌더는 코드 분석으로 확정하기 전까지 None 이다. 확정 전에는 배포하지 않는다.
    builder: Mapped[Builder | None] = mapped_column(enum_column(Builder, "builder"))
    dockerfile_path: Mapped[str | None] = mapped_column(String(255))
    platform: Mapped[str] = mapped_column(
        String(32), server_default="linux/amd64", default="linux/amd64"
    )
    railpack_version: Mapped[str | None] = mapped_column(String(32))
    analysis_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    port: Mapped[int | None] = mapped_column(Integer)
    build_command: Mapped[str | None] = mapped_column(Text)
    start_command: Mapped[str | None] = mapped_column(Text)
    # 서비스의 원하는 Pod 수와 Pod당 리소스. 배포 요청마다 스냅샷으로 고정한다.
    scaling_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 다음 배포부터 쓸 배포 방식. Pod 가 2개 미만이면 배포 요청이 ROLLING 으로 대체한다.
    deployment_strategy: Mapped[DeploymentStrategy] = mapped_column(
        enum_column(DeploymentStrategy, "deployment_strategy"),
        server_default=DeploymentStrategy.ROLLING.value,
        default=DeploymentStrategy.ROLLING,
    )
    kind: Mapped[ServiceKind] = mapped_column(
        enum_column(ServiceKind, "service_kind"),
        server_default=ServiceKind.APP.value,
        default=ServiceKind.APP,
    )
    # kind=DATABASE 일 때만 있다. 설정은 {image, storageGi, port, database, user} (camelCase).
    # 자격 증명은 여기 두지 않고 암호화한 서비스 변수(POSTGRES_PASSWORD 등)로 둔다.
    database_engine: Mapped[DatabaseEngine | None] = mapped_column(
        enum_column(DatabaseEngine, "database_engine")
    )
    database_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # 이 서비스 namespace 에 만들 호스트 별칭. [{name, targetServiceId, port}] (camelCase).
    # 코드가 compose 호스트명(api, postgres)을 그대로 쓰게 대상 서비스의 app Service 로 잇는다.
    host_aliases: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    # 같은 레포 분석에서 함께 만든 서비스 묶음과 그 안의 분석기 unit·dependency id.
    stack_id: Mapped[int | None] = mapped_column(ForeignKey("service_stacks.id"), index=True)
    stack_unit_id: Mapped[str | None] = mapped_column(String(200))

    @property
    def is_database(self) -> bool:
        return self.kind == ServiceKind.DATABASE
