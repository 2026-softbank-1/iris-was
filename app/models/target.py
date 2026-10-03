from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import TargetKind
from app.models.base import Base, BigIntPk, TimestampMixin, enum_column

# 서비스 기본 배포 타깃의 이름.
AWS_TARGET_NAME = "aws"


class Target(TimestampMixin, Base):
    """같은 이미지를 배포할 대상. 빌드는 한 번, 배포는 타깃마다 한 번이다."""

    __tablename__ = "targets"

    id: Mapped[BigIntPk]
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[TargetKind] = mapped_column(enum_column(TargetKind, "target_kind"))
    region: Mapped[str | None] = mapped_column(String(32))
    # 서비스 도메인 접미사. 예: aws.example.com
    domain_suffix: Mapped[str | None] = mapped_column(String(255))
    # 클러스터 접속 정보 자체가 아니라 비밀 저장소의 참조 이름만 둔다.
    cluster_ref: Mapped[str | None] = mapped_column(String(255))


class ServiceTarget(Base):
    """서비스가 배포되는 타깃."""

    __tablename__ = "service_targets"

    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), primary_key=True, index=True)
