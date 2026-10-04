"""add onprem server last seen at

등록한 온프레미스 서버가 마지막으로 인증에 성공한 시각(하트비트)을 남긴다. 비어 있으면(기능 전에
연결된 서버) API 는 저장한 상태를 그대로 알린다.

Revision ID: 1251511703f5
Revises: efb886b71ab8
Create Date: 2026-10-04 12:56:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1251511703f5"
down_revision: str | Sequence[str] | None = "efb886b71ab8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "onprem_servers", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("onprem_servers", "last_seen_at")
