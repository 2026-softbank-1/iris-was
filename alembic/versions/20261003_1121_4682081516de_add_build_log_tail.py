"""add build log tail

Revision ID: 4682081516de
Revises: 2dc16dd598ea
Create Date: 2026-10-03 11:21:54.233010

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4682081516de"
down_revision: str | Sequence[str] | None = "2dc16dd598ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "builds", sa.Column("log_tail", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("builds", "log_tail")
