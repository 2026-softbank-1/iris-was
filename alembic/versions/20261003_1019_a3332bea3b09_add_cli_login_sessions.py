"""add cli login sessions

Revision ID: a3332bea3b09
Revises: b8e1b90fa5a5
Create Date: 2026-10-03 10:19:00.353904

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3332bea3b09"
down_revision: str | Sequence[str] | None = "b8e1b90fa5a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "cli_login_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("poll_secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "DENIED",
                "EXPIRED",
                name="cli_login_session_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED')",
            name=op.f("ck_cli_login_sessions_cli_login_session_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_cli_login_sessions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cli_login_sessions")),
        sa.UniqueConstraint("public_id", name=op.f("uq_cli_login_sessions_public_id")),
    )
    op.create_index(
        "ix_cli_login_sessions_expires_at", "cli_login_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_cli_login_sessions_expires_at", table_name="cli_login_sessions")
    op.drop_table("cli_login_sessions")
