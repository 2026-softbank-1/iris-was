"""add database services and variable references

관리형 DB 서비스(services.kind=DATABASE, database_engine·database_config), 서비스 호스트 별칭
(services.host_aliases), 값 대신 다른 서비스의 연결 정보를 가리키는 참조 변수
(service_variables.reference)를 더한다. DB 서비스는 소스가 없어 github_installation_id 를 비울 수
있게 한다. 푸시 자동 배포가 환경변수 검증에서 막힌 요청을 남기도록 failure_code CHECK 제약에
VARIABLES_INVALID 를 더한다(값 목록이 바뀌어 지우고 다시 만든다).

Revision ID: 6b1f0c2d9a41
Revises: 061a382166d5
Create Date: 2026-10-04 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b1f0c2d9a41"
down_revision: str | Sequence[str] | None = "061a382166d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FAILURE_CODES = (
    "SOURCE_NOT_ACCESSIBLE",
    "SOURCE_REF_NOT_FOUND",
    "SOURCE_TOO_LARGE",
    "SOURCE_INVALID",
    "BUILD_CONFIG_REQUIRED",
    "BUILD_FAILED",
    "BUILD_TIMED_OUT",
    "BUILD_INFRA_ERROR",
    "DEPLOY_FAILED",
    "DEPLOY_TIMED_OUT",
    "DEPLOY_INFRA_ERROR",
)
_NEW_FAILURE_CODES = (*_OLD_FAILURE_CODES, "VARIABLES_INVALID")
_FAILURE_CODE_TABLES = (
    "deployment_requests",
    "deployment_status_histories",
    "builds",
    "releases",
)
_SERVICE_KINDS = ("APP", "DATABASE")
_DATABASE_ENGINES = ("postgres", "mysql", "mongodb", "redis")


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_failure_code_checks(values: tuple[str, ...]) -> None:
    for table in _FAILURE_CODE_TABLES:
        constraint = op.f(f"ck_{table}_failure_code")
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _in_list("failure_code", values))


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "services",
        sa.Column(
            "kind", _enum("service_kind", _SERVICE_KINDS), server_default="APP", nullable=False
        ),
    )
    op.add_column(
        "services",
        sa.Column("database_engine", _enum("database_engine", _DATABASE_ENGINES), nullable=True),
    )
    op.add_column(
        "services",
        sa.Column("database_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "services",
        sa.Column("host_aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column(
        "services", "github_installation_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.add_column(
        "service_variables",
        sa.Column("reference", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column("service_variables", "encrypted_value", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        op.f("ck_service_variables_value_or_reference"),
        "service_variables",
        "(encrypted_value IS NULL) <> (reference IS NULL)",
    )
    _replace_failure_code_checks(_NEW_FAILURE_CODES)


def downgrade() -> None:
    """Downgrade schema.

    참조 변수·DB 서비스·VARIABLES_INVALID 로 끝난 요청이 남아 있으면 NOT NULL·CHECK 제약을 다시
    만들 수 없어 실패한다. 사용자 데이터·배포 이력이라 지우지 않으니 운영자가 먼저 정리한다.
    """
    _replace_failure_code_checks(_OLD_FAILURE_CODES)
    op.drop_constraint(
        op.f("ck_service_variables_value_or_reference"), "service_variables", type_="check"
    )
    op.alter_column("service_variables", "encrypted_value", existing_type=sa.Text(), nullable=False)
    op.drop_column("service_variables", "reference")
    op.alter_column(
        "services", "github_installation_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_column("services", "host_aliases")
    op.drop_column("services", "database_config")
    op.drop_column("services", "database_engine")
    op.drop_column("services", "kind")
