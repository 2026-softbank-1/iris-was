import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 제약 이름을 고정해야 autogenerate 가 제약 변경을 감지한다.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Enum 은 varchar 에 값(value)으로 저장한다. 값을 추가해도 마이그레이션이 필요 없다.
    type_annotation_map = {
        enum.Enum: Enum(
            enum.Enum,
            native_enum=False,
            length=32,
            values_callable=lambda members: [member.value for member in members],
        ),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
