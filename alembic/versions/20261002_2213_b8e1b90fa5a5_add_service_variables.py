"""add service variables

Revision ID: b8e1b90fa5a5
Revises: 498894d5a55d
Create Date: 2026-10-02 22:13:16.225194

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e1b90fa5a5"
down_revision: str | Sequence[str] | None = "498894d5a55d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "service_variables",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
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
            ["service_id"], ["services.id"], name=op.f("fk_service_variables_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_variables")),
    )
    op.create_index(
        "uq_service_variables_service_id_key",
        "service_variables",
        ["service_id", "key"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_service_variables_service_id_key", table_name="service_variables")
    op.drop_table("service_variables")
