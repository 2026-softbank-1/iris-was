"""single deploy target per service

서비스는 타깃 하나에만 배포한다.

1. 기본값으로 모든 타깃이 붙은 기존 서비스에서 `aws` 가 있으면 `onprem` 연결을 지운다.
2. `onprem` 에만 연결됐지만 배포 요청이 있는 서비스는 `aws` 로 옮긴다. 이전에는 AWS 타깃에만
   배포했으므로(`services/{id}/prod`) 실제로 떠 있는 곳에 맞춘다.
3. `onprem` 타깃에 도메인 접미사를 정한다.

Revision ID: fe6c22271622
Revises: 72be38b96d7d
Create Date: 2026-10-03 14:50:39.570727

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fe6c22271622"
down_revision: str | Sequence[str] | None = "72be38b96d7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONPREM_DOMAIN_SUFFIX = "internal.likelion.uk"


def upgrade() -> None:
    op.execute(
        text(
            "DELETE FROM service_targets st USING targets o "
            "WHERE st.target_id = o.id AND o.name = 'onprem' AND EXISTS ("
            " SELECT 1 FROM service_targets a JOIN targets t ON t.id = a.target_id"
            " WHERE a.service_id = st.service_id AND t.name = 'aws')"
        )
    )
    op.execute(
        text(
            "UPDATE service_targets st SET target_id = a.id FROM targets o, targets a "
            "WHERE st.target_id = o.id AND o.name = 'onprem' AND a.name = 'aws' "
            "AND EXISTS (SELECT 1 FROM deployment_requests d WHERE d.service_id = st.service_id)"
        )
    )
    op.execute(
        text(
            "UPDATE targets SET domain_suffix = :suffix "
            "WHERE name = 'onprem' AND domain_suffix IS NULL"
        ).bindparams(suffix=ONPREM_DOMAIN_SUFFIX)
    )


def downgrade() -> None:
    """Downgrade schema.

    근사 복원이다. `aws` 가 붙은 서비스에 `onprem` 을 다시 붙여 이전 기본값(모든 타깃)으로 되돌린다.
    2 에서 `aws` 로 옮긴 서비스는 `onprem` 만 붙어 있던 상태로 돌아가지 않고 두 타깃을 모두 갖는다.
    """
    op.execute(
        text(
            "UPDATE targets SET domain_suffix = NULL WHERE name = 'onprem' "
            "AND domain_suffix = :suffix"
        ).bindparams(suffix=ONPREM_DOMAIN_SUFFIX)
    )
    op.execute(
        text(
            "INSERT INTO service_targets (service_id, target_id) "
            "SELECT a.service_id, o.id FROM service_targets a "
            "JOIN targets t ON t.id = a.target_id AND t.name = 'aws' "
            "CROSS JOIN targets o WHERE o.name = 'onprem' ON CONFLICT DO NOTHING"
        )
    )
