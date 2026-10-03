"""add deployment strategy

서비스가 고르는 배포 방식(services.deployment_strategy, 기본 ROLLING)과 배포 요청마다 남기는
요청 방식·적용 방식(deployment_requests.requested_deployment_strategy·deployment_strategy)을
더한다. 기존 서비스는 ROLLING 이 되고, 기존 배포 요청은 둘 다 비어 있다.

Revision ID: f49792bf1fcc
Revises: fe6c22271622
Create Date: 2026-10-03 23:34:47.475445

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f49792bf1fcc"
down_revision: str | Sequence[str] | None = "fe6c22271622"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRATEGIES = ("ROLLING", "CANARY", "BLUE_GREEN")


def _strategy(name: str) -> sa.Enum:
    return sa.Enum(*_STRATEGIES, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "deployment_requests",
        sa.Column(
            "requested_deployment_strategy",
            _strategy("requested_deployment_strategy"),
            nullable=True,
        ),
    )
    op.add_column(
        "deployment_requests",
        sa.Column("deployment_strategy", _strategy("deployment_strategy"), nullable=True),
    )
    op.add_column(
        "services",
        sa.Column(
            "deployment_strategy",
            _strategy("deployment_strategy"),
            server_default="ROLLING",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("services", "deployment_strategy")
    op.drop_column("deployment_requests", "deployment_strategy")
    op.drop_column("deployment_requests", "requested_deployment_strategy")
