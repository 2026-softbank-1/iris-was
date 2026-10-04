"""add onprem servers and target owner

사용자가 직접 등록하는 온프레미스 서버(onprem_servers)와, 서버마다 하나씩 생기는 전용 타깃을
표시하는 targets.owner_id 를 더한다. 타깃도 서버와 함께 지우므로 소프트 삭제 컬럼을 더한다.
기존 타깃(aws·onprem)은 owner_id 가 비어 있는 공용 타깃으로 남는다.

downgrade 는 스키마만 되돌린다. 그 전에 등록한 서버의 타깃 행은 owner_id 없이 남아 공용 타깃처럼
보이므로, 운영 데이터가 있으면 되돌리기 전에 그 행과 연결을 먼저 정리한다.

Revision ID: e98de0d34fa3
Revises: f49792bf1fcc
Create Date: 2026-10-04 00:33:03.758000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e98de0d34fa3"
down_revision: str | Sequence[str] | None = "f49792bf1fcc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("PENDING", "REGISTERING", "CONNECTED", "FAILED")
_FAILURE_CODES = ("CONNECT_TIMED_OUT", "GITOPS_COMMIT_FAILED")


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    # CHECK 제약은 아래에서 이름을 붙여 따로 만든다.
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False, length=32)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("targets", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "targets",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("targets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_targets_owner_id"), "targets", ["owner_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_targets_owner_id_users"), "targets", "users", ["owner_id"], ["id"]
    )

    op.create_table(
        "onprem_servers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("server_key", sa.String(length=8), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("status", _enum(_STATUSES, "onprem_server_status"), nullable=False),
        sa.Column(
            "failure_code", _enum(_FAILURE_CODES, "onprem_server_failure_code"), nullable=True
        ),
        sa.Column("registration_token_hash", sa.String(length=64), nullable=False),
        sa.Column("registration_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tailnet_fqdn", sa.String(length=255), nullable=True),
        sa.Column("api_ca_cert", sa.Text(), nullable=True),
        sa.Column("encrypted_service_account_token", sa.Text(), nullable=True),
        sa.Column("sealed_secrets_cert", sa.Text(), nullable=True),
        sa.Column("server_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("connect_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("gitops_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("gitops_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("connect_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _in("status", _STATUSES), name=op.f("ck_onprem_servers_onprem_server_status")
        ),
        sa.CheckConstraint(
            _in("failure_code", _FAILURE_CODES),
            name=op.f("ck_onprem_servers_onprem_server_failure_code"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_onprem_servers_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["targets.id"], name=op.f("fk_onprem_servers_target_id_targets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onprem_servers")),
        sa.UniqueConstraint(
            "registration_token_hash", name=op.f("uq_onprem_servers_registration_token_hash")
        ),
        sa.UniqueConstraint("server_key", name=op.f("uq_onprem_servers_server_key")),
        sa.UniqueConstraint(
            "server_secret_hash", name=op.f("uq_onprem_servers_server_secret_hash")
        ),
        sa.UniqueConstraint("target_id", name=op.f("uq_onprem_servers_target_id")),
    )
    op.create_index(
        "ix_onprem_servers_next_check_at",
        "onprem_servers",
        ["next_check_at"],
        unique=False,
        postgresql_where=sa.text("next_check_at IS NOT NULL"),
    )
    op.create_index(
        "uq_onprem_servers_owner_id_name",
        "onprem_servers",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_onprem_servers_owner_id_name", table_name="onprem_servers")
    op.drop_index("ix_onprem_servers_next_check_at", table_name="onprem_servers")
    op.drop_table("onprem_servers")
    op.drop_constraint(op.f("fk_targets_owner_id_users"), "targets", type_="foreignkey")
    op.drop_index(op.f("ix_targets_owner_id"), table_name="targets")
    op.drop_column("targets", "deleted_at")
    op.drop_column("targets", "is_deleted")
    op.drop_column("targets", "owner_id")
