"""rename local target to onprem

로컬 머신 배포를 없애고 on-prem 클러스터 타깃이 그 자리를 대신한다. 타깃 `local`(kind `LOCAL`)을
`onprem`(kind `ONPREM`)으로 바꾼다. kind CHECK 제약은 값 목록이 바뀌므로 지우고 다시 만든다.

Revision ID: 72be38b96d7d
Revises: 9f81c52a01bd
Create Date: 2026-10-03 14:50:28.650232

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "72be38b96d7d"
down_revision: str | Sequence[str] | None = "9f81c52a01bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CHECK = "ck_targets_target_kind"


def _rename(old: tuple[str, str], new: tuple[str, str]) -> None:
    (old_name, old_kind), (new_name, new_kind) = old, new
    op.drop_constraint(op.f(_KIND_CHECK), "targets", type_="check")
    op.execute(
        text("UPDATE targets SET kind = :new WHERE kind = :old").bindparams(
            new=new_kind, old=old_kind
        )
    )
    op.execute(
        text("UPDATE targets SET name = :new WHERE name = :old").bindparams(
            new=new_name, old=old_name
        )
    )
    op.create_check_constraint(op.f(_KIND_CHECK), "targets", f"kind IN ('AWS', '{new_kind}')")


def upgrade() -> None:
    """Upgrade schema."""
    _rename(("local", "LOCAL"), ("onprem", "ONPREM"))


def downgrade() -> None:
    """Downgrade schema."""
    _rename(("onprem", "ONPREM"), ("local", "LOCAL"))
