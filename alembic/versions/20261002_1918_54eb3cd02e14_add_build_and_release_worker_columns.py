"""add build and release worker columns

Build·Deploy Worker 가 쓰는 컬럼과 제약을 추가한다. enum CHECK 제약은 값 목록이 바뀌므로
지우고 다시 만든다. releases 에 build_id 를 NOT NULL 로 추가하므로 releases 가 비어 있어야 한다
(이 revision 전에는 Worker 가 없어 release 가 만들어질 수 없다).

Revision ID: 54eb3cd02e14
Revises: baaea3f724c5
Create Date: 2026-10-02 19:18:53.731865

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54eb3cd02e14"
down_revision: str | Sequence[str] | None = "baaea3f724c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_DEPLOYMENT_STATUSES = (
    "QUEUED",
    "BUILDING",
    "DEPLOYING",
    "SUCCEEDED",
    "FAILED",
    "ROLLED_BACK",
    "MANUAL_INTERVENTION",
)
_NEW_DEPLOYMENT_STATUSES = (*_OLD_DEPLOYMENT_STATUSES, "SUPERSEDED")
_OLD_RELEASE_STATUSES = ("PENDING", "SUCCEEDED", "FAILED", "ROLLED_BACK")
_NEW_RELEASE_STATUSES = ("PENDING", "SUCCEEDED", "FAILED", "ROLLING_BACK", "ROLLED_BACK")
_OLD_FAILURE_CODES = ("BUILD_CONFIG_REQUIRED", "BUILD_FAILED", "DEPLOY_FAILED")
_NEW_FAILURE_CODES = (
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
_BUILD_STATUSES = ("PENDING", "SNAPSHOTTING", "BUILDING", "SUCCEEDED", "FAILED", "CANCELLED")

# (테이블, 컬럼, 제약 이름 접미사)
_REPLACED_CHECKS = (
    ("deployment_requests", "status", "deployment_status"),
    ("deployment_requests", "failure_code", "failure_code"),
    ("deployment_status_histories", "from_status", "from_status"),
    ("deployment_status_histories", "to_status", "to_status"),
    ("deployment_status_histories", "failure_code", "failure_code"),
    ("releases", "status", "release_status"),
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_checks(*, upgrade: bool) -> None:
    """enum CHECK 제약의 값 목록을 새 값(upgrade) 또는 이전 값(downgrade)으로 바꾼다."""
    statuses = _NEW_DEPLOYMENT_STATUSES if upgrade else _OLD_DEPLOYMENT_STATUSES
    releases = _NEW_RELEASE_STATUSES if upgrade else _OLD_RELEASE_STATUSES
    failures = _NEW_FAILURE_CODES if upgrade else _OLD_FAILURE_CODES
    for table, column, name in _REPLACED_CHECKS:
        if name in ("deployment_status", "from_status", "to_status"):
            values = statuses
        elif name == "release_status":
            values = releases
        else:
            values = failures
        constraint = op.f(f"ck_{table}_{name}")
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _in_list(column, values))


def upgrade() -> None:
    """Upgrade schema."""
    _replace_checks(upgrade=True)

    op.add_column(
        "builds",
        sa.Column(
            "status",
            sa.Enum(
                *_BUILD_STATUSES,
                name="build_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="PENDING",
            nullable=False,
        ),
    )
    # 기존 행을 채우기 위한 임시 기본값이다. 모델은 Python 기본값만 쓴다.
    op.alter_column("builds", "status", server_default=None)
    op.add_column("builds", sa.Column("source_sha", sa.String(length=64), nullable=True))
    op.add_column(
        "builds",
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("builds", sa.Column("image_tag", sa.String(length=128), nullable=True))
    op.add_column(
        "builds", sa.Column("deploy_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "builds",
        sa.Column(
            "failure_code",
            sa.Enum(
                *_NEW_FAILURE_CODES,
                name="failure_code",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.alter_column("builds", "builder", existing_type=sa.VARCHAR(length=32), nullable=True)

    op.add_column(
        "deployment_requests",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("releases", sa.Column("build_id", sa.BigInteger(), nullable=False))
    op.add_column("releases", sa.Column("revert_commit_sha", sa.String(length=64), nullable=True))
    op.add_column(
        "releases",
        sa.Column(
            "failure_code",
            sa.Enum(
                *_NEW_FAILURE_CODES,
                name="failure_code",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.add_column("releases", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("releases", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_releases_build_id_builds"), "releases", "builds", ["build_id"], ["id"]
    )
    op.create_index(
        "uq_releases_service_id_target_id_in_flight",
        "releases",
        ["service_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'ROLLING_BACK')"),
    )


def downgrade() -> None:
    """Downgrade schema.

    SUPERSEDED·ROLLING_BACK·새 failure_code 값을 가진 행이 있으면 CHECK 제약을 되돌릴 수 없다.
    """
    op.drop_index(
        "uq_releases_service_id_target_id_in_flight",
        table_name="releases",
        postgresql_where=sa.text("status IN ('PENDING', 'ROLLING_BACK')"),
    )
    op.drop_constraint(op.f("fk_releases_build_id_builds"), "releases", type_="foreignkey")
    op.drop_column("releases", "finished_at")
    op.drop_column("releases", "deadline_at")
    op.drop_column("releases", "failure_code")
    op.drop_column("releases", "revert_commit_sha")
    op.drop_column("releases", "build_id")

    op.drop_column("deployment_requests", "cancel_requested_at")

    op.alter_column("builds", "builder", existing_type=sa.VARCHAR(length=32), nullable=False)
    op.drop_column("builds", "failure_code")
    op.drop_column("builds", "deploy_config")
    op.drop_column("builds", "image_tag")
    op.drop_column("builds", "attempt")
    op.drop_column("builds", "source_sha")
    op.drop_column("builds", "status")

    _replace_checks(upgrade=False)
