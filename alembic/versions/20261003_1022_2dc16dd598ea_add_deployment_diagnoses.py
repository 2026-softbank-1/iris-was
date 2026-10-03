"""add deployment diagnoses

Revision ID: 2dc16dd598ea
Revises: 71f880d9c6ae
Create Date: 2026-10-03 10:22:03.871792

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2dc16dd598ea"
down_revision: str | Sequence[str] | None = "71f880d9c6ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_diagnoses",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                name="diagnosis_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_deployment_diagnoses_diagnosis_status"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_deployment_diagnoses_deployment_request_id_deployment_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_deployment_diagnoses_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_diagnoses")),
    )
    op.create_index(
        "ix_deployment_diagnoses_deployment_request_id_created_at",
        "deployment_diagnoses",
        ["deployment_request_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_deployment_diagnoses_deployment_request_id_running",
        "deployment_diagnoses",
        ["deployment_request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_deployment_diagnoses_deployment_request_id_running",
        table_name="deployment_diagnoses",
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.drop_index(
        "ix_deployment_diagnoses_deployment_request_id_created_at",
        table_name="deployment_diagnoses",
    )
    op.drop_table("deployment_diagnoses")
