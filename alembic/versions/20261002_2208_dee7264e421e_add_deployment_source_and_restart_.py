"""add deployment source and restart trigger

재배포·롤백·재시작이 따라가는 원본 배포 요청(source_deployment_request_id)을 추가하고,
배포 시작 방식 CHECK 제약에 RESTART 를 더한다. enum CHECK 제약은 값 목록이 바뀌므로
지우고 다시 만든다.

Revision ID: dee7264e421e
Revises: 8c77b62ef71e
Create Date: 2026-10-02 22:08:15.612955

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dee7264e421e"
down_revision: str | Sequence[str] | None = "8c77b62ef71e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TRIGGERS = ("MANUAL", "PUSH", "CLI", "REDEPLOY", "ROLLBACK")
_NEW_TRIGGERS = (*_OLD_TRIGGERS, "RESTART")
_TRIGGER_CHECK = "ck_deployment_requests_deployment_trigger"
_SOURCE_FK = "fk_deployment_requests_source_deployment_request_id"


def _in_list(values: tuple[str, ...]) -> str:
    return f"trigger_type IN ({', '.join(repr(value) for value in values)})"


def _replace_trigger_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(_TRIGGER_CHECK), "deployment_requests", type_="check")
    op.create_check_constraint(op.f(_TRIGGER_CHECK), "deployment_requests", _in_list(values))


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "deployment_requests",
        sa.Column("source_deployment_request_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        _SOURCE_FK,
        "deployment_requests",
        "deployment_requests",
        ["source_deployment_request_id"],
        ["id"],
    )
    _replace_trigger_check(_NEW_TRIGGERS)


def downgrade() -> None:
    """Downgrade schema.

    RESTART 요청이 남아 있으면 이전 CHECK 제약을 만들 수 없어 실패한다. 그 요청은 배포 이력이라
    지우지 않으니, 먼저 운영자가 처리해야 한다.
    """
    _replace_trigger_check(_OLD_TRIGGERS)
    op.drop_constraint(_SOURCE_FK, "deployment_requests", type_="foreignkey")
    op.drop_column("deployment_requests", "source_deployment_request_id")
