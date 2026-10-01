from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, Enum, Identity, MetaData, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 제약 이름을 고정해야 autogenerate 가 제약 변경을 감지한다.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

BigIntPk = Annotated[int, mapped_column(BigInteger, Identity(), primary_key=True)]


def now_utc() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    """StrEnum 의 값(code)을 VARCHAR + CHECK 로 저장한다.

    PostgreSQL ENUM 타입은 값 추가를 autogenerate 가 감지하지 못해 쓰지 않는다.
    """
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        # 값이 늘어도 컬럼을 바꾸지 않도록 최장 값 길이가 아닌 고정 길이를 쓴다.
        length=32,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    # DB 기본값(DDL)과 Python 기본값을 함께 둔다. UPDATE 직후 속성이 만료되지 않아
    # 비동기 세션에서 응답 변환 시 추가 조회가 필요 없다.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=now_utc, onupdate=now_utc
    )


class SoftDeleteMixin:
    is_deleted: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def mark_as_deleted(self) -> None:
        self.is_deleted = True
        self.deleted_at = now_utc()
