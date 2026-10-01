"""seed default targets

Revision ID: ad14af350d44
Revises: e2e4363a1f49
Create Date: 2026-10-01 02:32:32.942820

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ad14af350d44"
down_revision: str | Sequence[str] | None = "e2e4363a1f49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TARGET_NAMES = ("aws", "local")


def upgrade() -> None:
    """Upgrade schema."""
    targets = sa.table("targets", sa.column("name", sa.String), sa.column("kind", sa.String))
    op.execute(
        postgresql.insert(targets)
        .values([{"name": "aws", "kind": "AWS"}, {"name": "local", "kind": "LOCAL"}])
        .on_conflict_do_nothing(index_elements=["name"])
    )


def downgrade() -> None:
    """Downgrade schema.

    서비스가 타깃을 참조하고 있으면 FK 위반으로 실패한다. 그 경우 먼저 서비스 연결을 정리해야 한다.
    """
    targets = sa.table("targets", sa.column("name", sa.String))
    op.execute(sa.delete(targets).where(targets.c.name.in_(DEFAULT_TARGET_NAMES)))
