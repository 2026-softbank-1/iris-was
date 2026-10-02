from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import Builder
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
    )

    id: Mapped[BigIntPk]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    source_repository_url: Mapped[str] = mapped_column(String(500))
    github_installation_id: Mapped[int] = mapped_column(
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
