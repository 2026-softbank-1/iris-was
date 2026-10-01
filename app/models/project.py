from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, SoftDeleteMixin, TimestampMixin


class Project(TimestampMixin, SoftDeleteMixin, Base):
    """서비스를 묶는 단위."""

    __tablename__ = "projects"
    __table_args__ = (
        Index(
            "uq_projects_owner_id_name",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("NOT is_deleted"),
        ),
    )

    id: Mapped[BigIntPk]
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
