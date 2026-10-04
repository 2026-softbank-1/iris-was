"""add onprem metric samples

사용자가 등록한 온프레미스 서버가 1분마다 보내는 Pod 별 CPU·메모리 표본(onprem_metric_samples)을
더한다. 서비스 메트릭 조회와 7일 보존 정리용 index 를 둔다.

Revision ID: 39a10baad186
Revises: 1251511703f5
Create Date: 2026-10-04 13:42:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "39a10baad186"
down_revision: str | Sequence[str] | None = "1251511703f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "onprem_metric_samples",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("pod", sa.String(length=253), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_millicores", sa.Double(), nullable=False),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_onprem_metric_samples_service_id_services"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onprem_metric_samples")),
    )
    op.create_index(
        "ix_onprem_metric_samples_collected_at",
        "onprem_metric_samples",
        ["collected_at"],
        unique=False,
    )
    op.create_index(
        "ix_onprem_metric_samples_service_id_collected_at",
        "onprem_metric_samples",
        ["service_id", "collected_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_onprem_metric_samples_service_id_collected_at", table_name="onprem_metric_samples"
    )
    op.drop_index("ix_onprem_metric_samples_collected_at", table_name="onprem_metric_samples")
    op.drop_table("onprem_metric_samples")
