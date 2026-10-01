from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import Builder
from app.models.base import Base, TimestampMixin


class Service(TimestampMixin, Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str]
    slug: Mapped[str] = mapped_column(unique=True)
    github_repository_id: Mapped[int] = mapped_column(BigInteger, index=True)
    repository_full_name: Mapped[str]
    # GitHub 의 installation ID. 없으면 공개 레포로 보고 우리 조직 설치 토큰을 쓴다.
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    root_directory: Mapped[str] = mapped_column(server_default=".")
    builder: Mapped[Builder] = mapped_column(server_default=Builder.AUTO.value)
    dockerfile_path: Mapped[str | None]
    auto_deploy: Mapped[bool] = mapped_column(server_default="false")
