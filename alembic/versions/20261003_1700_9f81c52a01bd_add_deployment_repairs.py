"""add deployment repairs

Revision ID: 9f81c52a01bd
Revises: 020608170372
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9f81c52a01bd"
down_revision: str | Sequence[str] | None = "020608170372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_repairs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column("diagnosis_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("source_sha", sa.String(40), nullable=False),
        sa.Column("source_repository_url", sa.String(500), nullable=False),
        sa.Column("root_directory", sa.String(255), nullable=False),
        sa.Column("plan_ids", postgresql.JSONB(), nullable=False),
        sa.Column("diagnosis_result", postgresql.JSONB(), nullable=False),
        sa.Column("request_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation_started_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN_OUTCOME')",
            name=op.f("ck_deployment_repairs_repair_status"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_deployment_repairs_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_deployment_repairs_deployment_request_id_deployment_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["diagnosis_id"],
            ["deployment_diagnoses.id"],
            name=op.f("fk_deployment_repairs_diagnosis_id_deployment_diagnoses"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_deployment_repairs_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_repairs")),
    )
    op.create_index(
        "uq_deployment_repairs_service_id_idempotency_key",
        "deployment_repairs",
        ["service_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_deployment_repairs_deployment_request_id_running",
        "deployment_repairs",
        ["deployment_request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_deployment_repairs_deployment_request_id_running", table_name="deployment_repairs"
    )
    op.drop_index(
        "uq_deployment_repairs_service_id_idempotency_key", table_name="deployment_repairs"
    )
    op.drop_table("deployment_repairs")
