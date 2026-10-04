from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import TargetKind
from app.models.base import Base, BigIntPk, SoftDeleteMixin, TimestampMixin, enum_column

if TYPE_CHECKING:
    from app.models.onprem_server import OnpremServer

# 서비스 기본 배포 타깃의 이름.
AWS_TARGET_NAME = "aws"


class Target(TimestampMixin, SoftDeleteMixin, Base):
    """같은 이미지를 배포할 대상. 빌드는 한 번, 배포는 타깃마다 한 번이다.

    `owner_id` 가 없으면 모두가 쓰는 공용 타깃이고, 있으면 그 사용자가 등록한 온프레미스
    서버(`onprem_server`)의 전용 타깃이다.
    """

    __tablename__ = "targets"

    id: Mapped[BigIntPk]
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[TargetKind] = mapped_column(enum_column(TargetKind, "target_kind"))
    region: Mapped[str | None] = mapped_column(String(32))
    # 서비스 도메인 접미사. 예: aws.example.com
    domain_suffix: Mapped[str | None] = mapped_column(String(255))
    # 클러스터 접속 정보 자체가 아니라 비밀 저장소의 참조 이름만 둔다.
    cluster_ref: Mapped[str | None] = mapped_column(String(255))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)

    # 등록한 서버의 타깃이면 그 서버. 읽는 쪽이 selectinload 로 함께 읽는다.
    onprem_server: Mapped["OnpremServer | None"] = relationship(
        lazy="raise", viewonly=True, uselist=False
    )


class ServiceTarget(Base):
    """서비스가 배포되는 타깃."""

    __tablename__ = "service_targets"

    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), primary_key=True, index=True)
