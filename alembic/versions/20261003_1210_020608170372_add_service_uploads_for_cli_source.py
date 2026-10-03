"""add service uploads for cli source

`likelion up` 이 올린 소스 아카이브의 메타데이터 테이블(service_uploads)과, 배포 요청이 그
업로드를 가리키는 컬럼(deployment_requests.service_upload_id)을 만든다. 업로드를 소스로 쓰는
빌드가 실패할 수 있어 failure_code CHECK 제약에 SOURCE_INVALID 를 더한다. enum CHECK 제약은
값 목록이 바뀌므로 지우고 다시 만든다.

Revision ID: 020608170372
Revises: 4682081516de
Create Date: 2026-10-03 12:10:26.703152

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020608170372"
down_revision: str | Sequence[str] | None = "4682081516de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FAILURE_CODES = (
    "SOURCE_NOT_ACCESSIBLE",
    "SOURCE_REF_NOT_FOUND",
    "SOURCE_TOO_LARGE",
    "BUILD_CONFIG_REQUIRED",
    "BUILD_FAILED",
    "BUILD_TIMED_OUT",
    "BUILD_INFRA_ERROR",
    "DEPLOY_FAILED",
    "DEPLOY_TIMED_OUT",
    "DEPLOY_INFRA_ERROR",
)
_NEW_FAILURE_CODES = (*_OLD_FAILURE_CODES[:3], "SOURCE_INVALID", *_OLD_FAILURE_CODES[3:])

_FAILURE_CODE_TABLES = (
    "deployment_requests",
    "deployment_status_histories",
    "builds",
    "releases",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_failure_code_checks(values: tuple[str, ...]) -> None:
    for table in _FAILURE_CODE_TABLES:
        constraint = op.f(f"ck_{table}_failure_code")
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _in_list("failure_code", values))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "service_uploads",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_service_uploads_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name=op.f("fk_service_uploads_uploaded_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_uploads")),
        sa.UniqueConstraint("public_id", name=op.f("uq_service_uploads_public_id")),
    )
    op.create_index(
        "ix_service_uploads_expires_at", "service_uploads", ["expires_at"], unique=False
    )
    op.add_column(
        "deployment_requests", sa.Column("service_upload_id", sa.BigInteger(), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_deployment_requests_service_upload_id"),
        "deployment_requests",
        ["service_upload_id"],
    )
    op.create_foreign_key(
        op.f("fk_deployment_requests_service_upload_id_service_uploads"),
        "deployment_requests",
        "service_uploads",
        ["service_upload_id"],
        ["id"],
    )
    _replace_failure_code_checks(_NEW_FAILURE_CODES)


def downgrade() -> None:
    """Downgrade schema.

    SOURCE_INVALID 로 끝난 요청·빌드·이력이 남아 있으면 이전 CHECK 제약을 만들 수 없어 실패한다.
    배포 이력이라 지우지 않으니, 먼저 운영자가 처리해야 한다. 업로드 메타데이터와 요청과의 연결은
    테이블과 함께 사라진다(S3 의 아카이브는 lifecycle 이 지운다).
    """
    _replace_failure_code_checks(_OLD_FAILURE_CODES)
    op.drop_constraint(
        op.f("fk_deployment_requests_service_upload_id_service_uploads"),
        "deployment_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_deployment_requests_service_upload_id"), "deployment_requests", type_="unique"
    )
    op.drop_column("deployment_requests", "service_upload_id")
    op.drop_index("ix_service_uploads_expires_at", table_name="service_uploads")
    op.drop_table("service_uploads")
