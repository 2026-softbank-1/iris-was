"""seed aws target domain suffix

`aws` 타깃의 도메인 접미사를 `likelion.uk` 로 정한다. 서비스 도메인 조회 API 가 이 값으로
주소를 계산한다. 값은 Deploy Worker 의 `BASE_DOMAIN` 과 같아야 두 주소가 어긋나지 않는다.
이미 접미사가 있는 타깃은 건드리지 않는다.

Revision ID: 8c77b62ef71e
Revises: 54eb3cd02e14
Create Date: 2026-10-02 21:55:00.000000

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c77b62ef71e"
down_revision: str | Sequence[str] | None = "54eb3cd02e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AWS_DOMAIN_SUFFIX = "likelion.uk"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        text(
            "UPDATE targets SET domain_suffix = :suffix "
            "WHERE name = 'aws' AND domain_suffix IS NULL"
        ).bindparams(suffix=AWS_DOMAIN_SUFFIX)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        text(
            "UPDATE targets SET domain_suffix = NULL WHERE name = 'aws' AND domain_suffix = :suffix"
        ).bindparams(suffix=AWS_DOMAIN_SUFFIX)
    )
