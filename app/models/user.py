from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, TimestampMixin


class User(TimestampMixin, Base):
    """GitHub 계정으로 로그인한 사용자."""

    __tablename__ = "users"

    id: Mapped[BigIntPk]
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)


class GithubInstallation(TimestampMixin, Base):
    """GitHub App 설치. 한 설치를 여러 사용자가 접근할 수 있다(조직 설치)."""

    __tablename__ = "github_installations"

    id: Mapped[BigIntPk]
    installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    account_login: Mapped[str] = mapped_column(String(255))
    # GitHub 가 주는 값 그대로 저장한다 ("User" · "Organization").
    account_type: Mapped[str] = mapped_column(String(32))


class UserGithubInstallation(Base):
    """사용자가 접근할 수 있는 GitHub App 설치. 로그인할 때마다 GitHub 기준으로 맞춘다."""

    __tablename__ = "user_github_installations"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    github_installation_id: Mapped[int] = mapped_column(
        ForeignKey("github_installations.id"), primary_key=True
    )
