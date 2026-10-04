"""allow gcp target kind

GKE 워크로드 클러스터에 배포하는 타깃 종류 GCP 를 더한다(ADR 0036). 값 목록이 바뀌어 CHECK 제약을
지우고 다시 만든다.

Revision ID: 3c7e9a1b5d20
Revises: 39a10baad186
Create Date: 2026-10-04 14:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7e9a1b5d20"
down_revision: str | Sequence[str] | None = "39a10baad186"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = ("AWS", "ONPREM")
_NEW_KINDS = (*_OLD_KINDS, "GCP")


def _replace_check(values: tuple[str, ...]) -> None:
    constraint = op.f("ck_targets_target_kind")
    op.drop_constraint(constraint, "targets", type_="check")
    op.create_check_constraint(
        constraint, "targets", f"kind IN ({', '.join(repr(value) for value in values)})"
    )


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check(_NEW_KINDS)


def downgrade() -> None:
    """Downgrade schema.

    GCP 타깃 행이 남아 있으면 제약 생성이 실패한다. 먼저 seed revision 을 되돌린다.
    """
    _replace_check(_OLD_KINDS)
