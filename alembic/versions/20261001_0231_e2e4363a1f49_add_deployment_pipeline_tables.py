"""add deployment pipeline tables

Revision ID: e2e4363a1f49
Revises: da3066d208c9
Create Date: 2026-10-01 02:31:50.307091

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2e4363a1f49"
down_revision: str | Sequence[str] | None = "da3066d208c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "environment",
            sa.Enum(
                "prod", name="environment", native_enum=False, create_constraint=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("source_sha", sa.String(length=64), nullable=False),
        sa.Column("source_commit_message", sa.Text(), nullable=True),
        sa.Column(
            "trigger_type",
            sa.Enum(
                "MANUAL",
                "PUSH",
                "CLI",
                "REDEPLOY",
                "ROLLBACK",
                name="deployment_trigger",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "BUILDING",
                "DEPLOYING",
                "SUCCEEDED",
                "FAILED",
                "ROLLED_BACK",
                "MANUAL_INTERVENTION",
                name="deployment_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "failure_code",
            sa.Enum(
                "BUILD_CONFIG_REQUIRED",
                "BUILD_FAILED",
                "DEPLOY_FAILED",
                name="failure_code",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("variables_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "environment IN ('prod')", name=op.f("ck_deployment_requests_environment")
        ),
        sa.CheckConstraint(
            "failure_code IN ('BUILD_CONFIG_REQUIRED', 'BUILD_FAILED', 'DEPLOY_FAILED')",
            name=op.f("ck_deployment_requests_failure_code"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'DEPLOYING', 'SUCCEEDED', 'FAILED', "
            "'ROLLED_BACK', 'MANUAL_INTERVENTION')",
            name=op.f("ck_deployment_requests_deployment_status"),
        ),
        sa.CheckConstraint(
            "trigger_type IN ('MANUAL', 'PUSH', 'CLI', 'REDEPLOY', 'ROLLBACK')",
            name=op.f("ck_deployment_requests_deployment_trigger"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_deployment_requests_requested_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_deployment_requests_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_deployment_requests_idempotency_key")),
    )
    op.create_index(
        "ix_deployment_requests_service_id_created_at",
        "deployment_requests",
        ["service_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_deployment_requests_active",
        "deployment_requests",
        ["service_id", "environment"],
        unique=True,
        postgresql_where="status IN ('QUEUED', 'BUILDING', 'DEPLOYING')",
    )
    op.create_table(
        "builds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "builder",
            sa.Enum(
                "dockerfile",
                "railpack",
                name="builder",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("codebuild_build_id", sa.String(length=255), nullable=True),
        sa.Column("image_repository", sa.String(length=500), nullable=True),
        sa.Column("image_digest", sa.String(length=80), nullable=True),
        sa.Column("log_url", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("builder IN ('dockerfile', 'railpack')", name=op.f("ck_builds_builder")),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_builds_deployment_request_id_deployment_requests"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_builds")),
        sa.UniqueConstraint("codebuild_build_id", name=op.f("uq_builds_codebuild_build_id")),
        sa.UniqueConstraint("deployment_request_id", name=op.f("uq_builds_deployment_request_id")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "BUILD",
                "DEPLOY",
                "RECONCILE",
                "ROLLBACK",
                name="job_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "RETRY_WAIT",
                "FAILED",
                "MANUAL_INTERVENTION",
                name="job_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint(
            "kind IN ('BUILD', 'DEPLOY', 'RECONCILE', 'ROLLBACK')", name=op.f("ck_jobs_job_kind")
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'RETRY_WAIT', 'FAILED', "
            "'MANUAL_INTERVENTION')",
            name=op.f("ck_jobs_job_status"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_jobs_deployment_request_id_deployment_requests"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        op.f("ix_jobs_deployment_request_id"), "jobs", ["deployment_request_id"], unique=False
    )
    op.create_index(
        "ix_jobs_queued",
        "jobs",
        [sa.literal_column("priority DESC"), "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_table(
        "releases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "environment",
            sa.Enum(
                "prod", name="environment", native_enum=False, create_constraint=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("image_digest", sa.String(length=80), nullable=False),
        sa.Column("gitops_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("argo_sync_status", sa.String(length=32), nullable=True),
        sa.Column("argo_health_status", sa.String(length=32), nullable=True),
        sa.Column("previous_good_release_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "SUCCEEDED",
                "FAILED",
                "ROLLED_BACK",
                name="release_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
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
        sa.CheckConstraint("environment IN ('prod')", name=op.f("ck_releases_environment")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'ROLLED_BACK')",
            name=op.f("ck_releases_release_status"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_releases_deployment_request_id_deployment_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["previous_good_release_id"],
            ["releases.id"],
            name=op.f("fk_releases_previous_good_release_id_releases"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_releases_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["targets.id"], name=op.f("fk_releases_target_id_targets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_releases")),
    )
    op.create_index(
        op.f("ix_releases_deployment_request_id"),
        "releases",
        ["deployment_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_releases_last_known_good",
        "releases",
        ["service_id", "environment", "target_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_releases_last_known_good", table_name="releases")
    op.drop_index(op.f("ix_releases_deployment_request_id"), table_name="releases")
    op.drop_table("releases")
    op.drop_index(
        "ix_jobs_queued", table_name="jobs", postgresql_where=sa.text("status = 'QUEUED'")
    )
    op.drop_index(op.f("ix_jobs_deployment_request_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("builds")
    op.drop_index(
        "uq_deployment_requests_active",
        table_name="deployment_requests",
        postgresql_where="status IN ('QUEUED', 'BUILDING', 'DEPLOYING')",
    )
    op.drop_index("ix_deployment_requests_service_id_created_at", table_name="deployment_requests")
    op.drop_table("deployment_requests")
