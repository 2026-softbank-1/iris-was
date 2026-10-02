"""add remove job kind and trigger

서비스를 클러스터에서 내리는 삭제 요청을 위해 deployment_trigger 와 job_kind CHECK 제약에
REMOVE 를 더한다. enum CHECK 제약은 값 목록이 바뀌므로 지우고 다시 만든다.

Revision ID: 498894d5a55d
Revises: dee7264e421e
Create Date: 2026-10-02 23:20:18.371204

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "498894d5a55d"
down_revision: str | Sequence[str] | None = "dee7264e421e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TRIGGERS = ("MANUAL", "PUSH", "CLI", "REDEPLOY", "ROLLBACK", "RESTART")
_NEW_TRIGGERS = (*_OLD_TRIGGERS, "REMOVE")
_OLD_JOB_KINDS = ("BUILD", "DEPLOY", "RECONCILE", "ROLLBACK")
_NEW_JOB_KINDS = (*_OLD_JOB_KINDS, "REMOVE")

# (테이블, 컬럼, 제약 이름)
_TRIGGER_CHECK = (
    "deployment_requests",
    "trigger_type",
    "ck_deployment_requests_deployment_trigger",
)
_JOB_KIND_CHECK = ("jobs", "kind", "ck_jobs_job_kind")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(check: tuple[str, str, str], values: tuple[str, ...]) -> None:
    table, column, name = check
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, _in_list(column, values))


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check(_TRIGGER_CHECK, _NEW_TRIGGERS)
    _replace_check(_JOB_KIND_CHECK, _NEW_JOB_KINDS)


def downgrade() -> None:
    """Downgrade schema.

    REMOVE 요청이나 REMOVE job 이 남아 있으면 이전 CHECK 제약을 만들 수 없어 실패한다. 배포 이력과
    작업 기록이라 지우지 않으니, 먼저 운영자가 처리해야 한다.
    """
    _replace_check(_JOB_KIND_CHECK, _OLD_JOB_KINDS)
    _replace_check(_TRIGGER_CHECK, _OLD_TRIGGERS)
