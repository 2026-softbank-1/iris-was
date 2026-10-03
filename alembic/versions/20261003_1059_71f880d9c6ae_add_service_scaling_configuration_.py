"""add service scaling configuration snapshots

Revision ID: 71f880d9c6ae
Revises: b9b7e3e0333a
Create Date: 2026-10-03 10:59:41.131008

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "71f880d9c6ae"
down_revision: str | Sequence[str] | None = "b9b7e3e0333a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("services", sa.Column("scaling_config", postgresql.JSONB(), nullable=True))
    op.add_column(
        "deployment_requests", sa.Column("scaling_snapshot", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployment_requests", "scaling_snapshot")
    op.drop_column("services", "scaling_config")
