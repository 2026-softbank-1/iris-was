"""seed gcp target

모두가 쓰는 공용 GCP 타깃(`gcp`, GKE `gcp-dev-workload`)을 넣는다. 서비스 주소는
`{서비스 이름}-{service_id}.gcp.likelion.uk` 다(iris-infra contracts/gcp-target.md).

Revision ID: 8d2f4b6a0e13
Revises: 3c7e9a1b5d20
Create Date: 2026-10-04 14:31:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d2f4b6a0e13"
down_revision: str | Sequence[str] | None = "3c7e9a1b5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GCP_TARGET_NAME = "gcp"


def _targets() -> sa.TableClause:
    return sa.table(
        "targets",
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("region", sa.String),
        sa.column("domain_suffix", sa.String),
        sa.column("cluster_ref", sa.String),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        postgresql.insert(_targets())
        .values(
            name=GCP_TARGET_NAME,
            kind="GCP",
            region="asia-northeast3",
            domain_suffix="gcp.likelion.uk",
            cluster_ref="gcp-dev-workload",
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )


def downgrade() -> None:
    """Downgrade schema.

    서비스가 이 타깃을 참조하고 있으면 FK 위반으로 실패한다. 먼저 서비스 연결을 정리해야 한다.
    """
    targets = _targets()
    op.execute(sa.delete(targets).where(targets.c.name == GCP_TARGET_NAME))
