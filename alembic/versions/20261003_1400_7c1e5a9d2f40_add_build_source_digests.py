"""add build source digests

Revision ID: 7c1e5a9d2f40
Revises: 4682081516de
Create Date: 2026-10-03 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e5a9d2f40"
down_revision: str | Sequence[str] | None = "4682081516de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("builds", sa.Column("source_archive_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "builds", sa.Column("source_manifest_sha256", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("builds", "source_manifest_sha256")
    op.drop_column("builds", "source_archive_sha256")
