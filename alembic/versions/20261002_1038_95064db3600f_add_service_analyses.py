"""add service analyses

Revision ID: 95064db3600f
Revises: baaea3f724c5
Create Date: 2026-10-02 10:38:24.922521

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95064db3600f"
down_revision: str | Sequence[str] | None = "baaea3f724c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_analyses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("source_repository_url", sa.String(length=500), nullable=False),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("source_sha", sa.String(length=40), nullable=False),
        sa.Column("root_directory", sa.String(length=255), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=12), nullable=False),
        sa.Column("model_selection", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'QUEUED'"), nullable=False
        ),
        sa.Column(
            "stage", sa.String(length=64), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("context_hash", sa.String(length=128), nullable=True),
        sa.Column("result_digest", sa.String(length=128), nullable=True),
        sa.Column("analysis_status", sa.String(length=32), nullable=True),
        sa.Column("analysis_result", postgresql.JSONB(), nullable=True),
        sa.Column("verification_report", postgresql.JSONB(), nullable=True),
        sa.Column("source_readiness", postgresql.JSONB(), nullable=True),
        sa.Column("deployment_dossier", postgresql.JSONB(), nullable=True),
        sa.Column("run_report", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("builder_recommendation", sa.String(length=32), nullable=True),
        sa.Column("review_required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_service_candidate_id", sa.String(length=255), nullable=True),
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
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_service_analyses_analysis_job_status"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_service_analyses_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_service_analyses_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_analyses")),
    )
    op.create_index(
        "ix_service_analyses_latest", "service_analyses", ["service_id", sa.text("created_at DESC")]
    )
    op.create_index("ix_service_analyses_queue", "service_analyses", ["status", "locked_until"])
    op.create_index(
        "uq_service_analyses_active",
        "service_analyses",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_service_analyses_active", table_name="service_analyses")
    op.drop_index("ix_service_analyses_queue", table_name="service_analyses")
    op.drop_index("ix_service_analyses_latest", table_name="service_analyses")
    op.drop_table("service_analyses")
