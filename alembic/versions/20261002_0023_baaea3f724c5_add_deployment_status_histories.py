"""add deployment status histories

Revision ID: baaea3f724c5
Revises: ad14af350d44
Create Date: 2026-10-02 00:23:07.138862

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "baaea3f724c5"
down_revision: str | Sequence[str] | None = "ad14af350d44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEPLOYMENT_STATUSES = (
    "QUEUED",
    "BUILDING",
    "DEPLOYING",
    "SUCCEEDED",
    "FAILED",
    "ROLLED_BACK",
    "MANUAL_INTERVENTION",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_status_histories",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "from_status",
            sa.Enum(
                *_DEPLOYMENT_STATUSES,
                name="from_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                *_DEPLOYMENT_STATUSES,
                name="to_status",
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "failure_code IN ('BUILD_CONFIG_REQUIRED', 'BUILD_FAILED', 'DEPLOY_FAILED')",
            name=op.f("ck_deployment_status_histories_failure_code"),
        ),
        sa.CheckConstraint(
            "from_status IN ('QUEUED', 'BUILDING', 'DEPLOYING', 'SUCCEEDED', 'FAILED', "
            "'ROLLED_BACK', 'MANUAL_INTERVENTION')",
            name=op.f("ck_deployment_status_histories_from_status"),
        ),
        sa.CheckConstraint(
            "to_status IN ('QUEUED', 'BUILDING', 'DEPLOYING', 'SUCCEEDED', 'FAILED', "
            "'ROLLED_BACK', 'MANUAL_INTERVENTION')",
            name=op.f("ck_deployment_status_histories_to_status"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_deployment_status_histories_deployment_request_id_deployment_requests"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_status_histories")),
    )
    op.create_index(
        "ix_deployment_status_histories_deployment_request_id_created_at",
        "deployment_status_histories",
        ["deployment_request_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_deployment_status_histories_deployment_request_id_created_at",
        table_name="deployment_status_histories",
    )
    op.drop_table("deployment_status_histories")
