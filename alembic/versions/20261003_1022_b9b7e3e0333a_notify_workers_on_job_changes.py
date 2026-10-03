"""notify workers on job changes

job 이 생기거나 다시 대기열로 돌아갈 때, BUILD 가 끝날 때 `NOTIFY jobs, <kind>` 를 보내 Worker 를
깨운다. 알림은 commit 시점에만 나가고 rollback 되면 사라진다. 선점(QUEUED → RUNNING)·lease 갱신·
BUILD 외 job 의 종료는 알리지 않는다.
autogenerate 는 트리거를 감지하지 못해 직접 작성한다.

Revision ID: b9b7e3e0333a
Revises: a3332bea3b09
Create Date: 2026-10-03 10:22:21.756169

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9b7e3e0333a"
down_revision: str | Sequence[str] | None = "a3332bea3b09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE FUNCTION notify_job_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('jobs', NEW.kind);
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_jobs_notify_insert AFTER INSERT ON jobs "
        "FOR EACH ROW EXECUTE FUNCTION notify_job_change()"
    )
    # 반납·snooze·재시도는 Worker 가 대기 시간을 다시 계산하도록 알린다. 끝난 job 은 BUILD 만
    # 알린다. 사용자별 동시 빌드 제한에 막혀 있던 BUILD 가 선점 가능해진다.
    op.execute(
        "CREATE TRIGGER trg_jobs_notify_release AFTER UPDATE OF status ON jobs "
        "FOR EACH ROW WHEN (OLD.status = 'RUNNING' "
        "AND (NEW.status IN ('QUEUED', 'RETRY_WAIT') OR NEW.kind = 'BUILD')) "
        "EXECUTE FUNCTION notify_job_change()"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER trg_jobs_notify_release ON jobs")
    op.execute("DROP TRIGGER trg_jobs_notify_insert ON jobs")
    op.execute("DROP FUNCTION notify_job_change()")
