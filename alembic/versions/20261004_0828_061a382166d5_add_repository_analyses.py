"""add repository analyses

서비스를 만들기 전 레포 구성 분석(Analysis Gate) 1건을 담는 repository_analyses 를 만든다. Build
Worker 가 QUEUED 분석을 FOR UPDATE SKIP LOCKED 로 선점하고 lease(locked_until)가 만료된 RUNNING 을
다시 가져간다. 분석이 생기거나 대기열로 돌아갈 때 jobs 와 같은 채널로 `NOTIFY jobs,
REPOSITORY_ANALYSIS` 를 보내 Worker 를 깨운다. autogenerate 는 트리거를 감지하지 못해 직접 작성한다.

Revision ID: 061a382166d5
Revises: f49792bf1fcc
Create Date: 2026-10-04 08:28:20.665048

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "061a382166d5"
down_revision: str | Sequence[str] | None = "f49792bf1fcc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "repository_analyses"
_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "APPLIED")
_MODES = ("auto", "force")
_DECISIONS = ("skip", "analyze")
_COMPLEXITIES = ("simple", "complex", "unsupported")
_ERROR_CODES = (
    "SOURCE_NOT_ACCESSIBLE",
    "SOURCE_REF_NOT_FOUND",
    "SOURCE_TOO_LARGE",
    "SOURCE_INVALID",
    "ANALYZER_UNAVAILABLE",
    "ANALYZER_TIMED_OUT",
    "ANALYZER_FAILED",
    "ANALYSIS_INTERRUPTED",
)


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False, length=32)


def _check(column: str, name: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{_TABLE}_{name}"))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_repository_url", sa.String(length=500), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("source_sha", sa.String(length=64), nullable=True),
        sa.Column("root_directory", sa.String(length=255), nullable=True),
        sa.Column("mode", _enum("mode", _MODES), nullable=False),
        sa.Column(
            "status",
            _enum("repository_analysis_status", _STATUSES),
            server_default="QUEUED",
            nullable=False,
        ),
        sa.Column("decision", _enum("decision", _DECISIONS), nullable=True),
        sa.Column("complexity", _enum("complexity", _COMPLEXITIES), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", _enum("error_code", _ERROR_CODES), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("applied_service_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _check("mode", "mode", _MODES),
        _check("status", "repository_analysis_status", _STATUSES),
        _check("decision", "decision", _DECISIONS),
        _check("complexity", "complexity", _COMPLEXITIES),
        _check("error_code", "error_code", _ERROR_CODES),
        sa.ForeignKeyConstraint(
            ["github_installation_id"],
            ["github_installations.id"],
            name=op.f("fk_repository_analyses_github_installation_id_github_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_repository_analyses_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_repository_analyses_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_analyses")),
    )
    op.create_index(
        "ix_repository_analyses_pending",
        _TABLE,
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(op.f("ix_repository_analyses_project_id"), _TABLE, ["project_id"], unique=False)
    op.create_index(op.f("ix_repository_analyses_user_id"), _TABLE, ["user_id"], unique=False)
    op.execute(
        """
        CREATE FUNCTION notify_repository_analysis_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('jobs', 'REPOSITORY_ANALYSIS');
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_repository_analyses_notify_insert AFTER INSERT ON repository_analyses "
        "FOR EACH ROW EXECUTE FUNCTION notify_repository_analysis_change()"
    )
    # 종료 신호로 반납한 분석은 다른 Worker 가 바로 이어 가도록 알린다.
    op.execute(
        "CREATE TRIGGER trg_repository_analyses_notify_release "
        "AFTER UPDATE OF status ON repository_analyses "
        "FOR EACH ROW WHEN (OLD.status = 'RUNNING' AND NEW.status = 'QUEUED') "
        "EXECUTE FUNCTION notify_repository_analysis_change()"
    )


def downgrade() -> None:
    """Downgrade schema.

    분석 기록과 거기 남은 분석기 결과가 함께 사라진다. 서비스는 analysis_plan 에 근거를 복사해 두어
    영향이 없다.
    """
    op.execute("DROP TRIGGER trg_repository_analyses_notify_release ON repository_analyses")
    op.execute("DROP TRIGGER trg_repository_analyses_notify_insert ON repository_analyses")
    op.execute("DROP FUNCTION notify_repository_analysis_change()")
    op.drop_index(op.f("ix_repository_analyses_user_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_repository_analyses_project_id"), table_name=_TABLE)
    op.drop_index(
        "ix_repository_analyses_pending",
        table_name=_TABLE,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.drop_table(_TABLE)
