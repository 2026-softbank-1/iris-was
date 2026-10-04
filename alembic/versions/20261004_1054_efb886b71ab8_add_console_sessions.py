"""add console sessions

서비스 콘솔(Pod 셸) 연결 ticket 을 발급한 감사 기록 테이블(console_sessions)을 만든다. 쓰기만 하고
고치지 않으며 지우지 않는다(ADR 0033).

Revision ID: efb886b71ab8
Revises: 663b24ad4296
Create Date: 2026-10-04 10:54:46.171894

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "efb886b71ab8"
down_revision: str | Sequence[str] | None = "663b24ad4296"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "console_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("release_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["release_id"], ["releases.id"], name=op.f("fk_console_sessions_release_id_releases")
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_console_sessions_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["targets.id"], name=op.f("fk_console_sessions_target_id_targets")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_console_sessions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_console_sessions")),
        sa.UniqueConstraint("public_id", name=op.f("uq_console_sessions_public_id")),
    )
    op.create_index(
        "ix_console_sessions_service_id_created_at",
        "console_sessions",
        ["service_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_console_sessions_service_id_created_at", table_name="console_sessions")
    op.drop_table("console_sessions")
