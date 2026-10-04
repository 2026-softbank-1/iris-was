"""add service stacks

같은 레포 분석에서 만든 서비스(앱 + DB) 묶음(service_stacks)과 서비스의 소속
(services.stack_id·stack_unit_id), 그 묶음을 의존 순서대로 배포하는 스택 배포
(stack_deployments·stack_deployment_steps), 푸시가 다시 접수한 스택 재분석의 소속
(repository_analyses.stack_id)을 만든다. 앞 단계가 실패해 시작하지 않은 배포 요청을 남기도록
failure_code CHECK 제약에 DEPENDENCY_FAILED 를 더한다.

service_stacks.analysis_id 와 repository_analyses.stack_id 가 서로를 가리키므로 테이블을 만든 뒤
repository_analyses 쪽 외래 키를 단다.

Revision ID: c4e2a7b81f30
Revises: 6b1f0c2d9a41
Create Date: 2026-10-04 09:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e2a7b81f30"
down_revision: str | Sequence[str] | None = "6b1f0c2d9a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FAILURE_CODES = (
    "SOURCE_NOT_ACCESSIBLE",
    "SOURCE_REF_NOT_FOUND",
    "SOURCE_TOO_LARGE",
    "SOURCE_INVALID",
    "BUILD_CONFIG_REQUIRED",
    "BUILD_FAILED",
    "BUILD_TIMED_OUT",
    "BUILD_INFRA_ERROR",
    "DEPLOY_FAILED",
    "DEPLOY_TIMED_OUT",
    "DEPLOY_INFRA_ERROR",
    "VARIABLES_INVALID",
)
_NEW_FAILURE_CODES = (*_OLD_FAILURE_CODES, "DEPENDENCY_FAILED")
_FAILURE_CODE_TABLES = (
    "deployment_requests",
    "deployment_status_histories",
    "builds",
    "releases",
)
_TRIGGERS = ("MANUAL", "PUSH", "CLI", "REDEPLOY", "ROLLBACK", "RESTART", "REMOVE")
_STEP_STATUSES = ("WAITING", "STARTED", "HELD")


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False, length=32)


def _check(table: str, column: str, name: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{name}"))


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
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
    ]


def _replace_failure_code_checks(values: tuple[str, ...]) -> None:
    allowed = ", ".join(repr(value) for value in values)
    for table in _FAILURE_CODE_TABLES:
        constraint = op.f(f"ck_{table}_failure_code")
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"failure_code IN ({allowed})")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "service_stacks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_repository_url", sa.String(length=500), nullable=False),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("root_directory", sa.String(length=255), nullable=True),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("analysis_id", sa.BigInteger(), nullable=False),
        sa.Column("pending_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pending_analysis_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["repository_analyses.id"],
            name=op.f("fk_service_stacks_analysis_id_repository_analyses"),
        ),
        sa.ForeignKeyConstraint(
            ["github_installation_id"],
            ["github_installations.id"],
            name=op.f("fk_service_stacks_github_installation_id_github_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["pending_analysis_id"],
            ["repository_analyses.id"],
            name=op.f("fk_service_stacks_pending_analysis_id_repository_analyses"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_service_stacks_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_stacks")),
    )
    op.create_index(
        op.f("ix_service_stacks_project_id"), "service_stacks", ["project_id"], unique=False
    )
    op.create_index(
        "ix_service_stacks_repository",
        "service_stacks",
        ["source_repository_url", "source_branch"],
        unique=False,
    )

    op.add_column("services", sa.Column("stack_id", sa.BigInteger(), nullable=True))
    op.add_column("services", sa.Column("stack_unit_id", sa.String(length=200), nullable=True))
    op.create_index(op.f("ix_services_stack_id"), "services", ["stack_id"], unique=False)
    op.create_index(
        "uq_services_stack_id_stack_unit_id",
        "services",
        ["stack_id", "stack_unit_id"],
        unique=True,
        postgresql_where=sa.text("stack_id IS NOT NULL AND NOT is_deleted"),
    )
    op.create_foreign_key(
        op.f("fk_services_stack_id_service_stacks"),
        "services",
        "service_stacks",
        ["stack_id"],
        ["id"],
    )

    op.add_column("repository_analyses", sa.Column("stack_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "uq_repository_analyses_stack_id_source_sha",
        "repository_analyses",
        ["stack_id", "source_sha"],
        unique=True,
        postgresql_where=sa.text("stack_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_repository_analyses_stack_id_service_stacks"),
        "repository_analyses",
        "service_stacks",
        ["stack_id"],
        ["id"],
    )

    op.create_table(
        "stack_deployments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("stack_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_type", _enum("deployment_trigger", _TRIGGERS), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        *_timestamps(),
        _check("stack_deployments", "trigger_type", "deployment_trigger", _TRIGGERS),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_stack_deployments_requested_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["stack_id"],
            ["service_stacks.id"],
            name=op.f("fk_stack_deployments_stack_id_service_stacks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stack_deployments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_stack_deployments_idempotency_key")),
    )
    op.create_index(
        op.f("ix_stack_deployments_stack_id"), "stack_deployments", ["stack_id"], unique=False
    )

    op.create_table(
        "stack_deployment_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("stack_deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_request_id", sa.BigInteger(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column(
            "depends_on_deployment_request_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", _enum("stack_deployment_step_status", _STEP_STATUSES), nullable=False),
        sa.Column("held_by_deployment_request_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        _check("stack_deployment_steps", "status", "stack_deployment_step_status", _STEP_STATUSES),
        sa.ForeignKeyConstraint(
            ["deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_stack_deployment_steps_deployment_request_id_deployment_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["held_by_deployment_request_id"],
            ["deployment_requests.id"],
            name=op.f(
                "fk_stack_deployment_steps_held_by_deployment_request_id_deployment_requests"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_stack_deployment_steps_service_id_services"),
        ),
        sa.ForeignKeyConstraint(
            ["stack_deployment_id"],
            ["stack_deployments.id"],
            name=op.f("fk_stack_deployment_steps_stack_deployment_id_stack_deployments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stack_deployment_steps")),
        sa.UniqueConstraint(
            "deployment_request_id", name=op.f("uq_stack_deployment_steps_deployment_request_id")
        ),
    )
    op.create_index(
        op.f("ix_stack_deployment_steps_stack_deployment_id"),
        "stack_deployment_steps",
        ["stack_deployment_id"],
        unique=False,
    )
    _replace_failure_code_checks(_NEW_FAILURE_CODES)


def downgrade() -> None:
    """Downgrade schema.

    DEPENDENCY_FAILED 로 끝난 배포 이력이 남아 있으면 이전 CHECK 제약을 만들 수 없어 실패한다.
    스택 소속·스택 배포 기록은 테이블과 함께 사라진다(서비스와 배포 요청은 남는다).
    """
    _replace_failure_code_checks(_OLD_FAILURE_CODES)
    op.drop_index(
        op.f("ix_stack_deployment_steps_stack_deployment_id"), table_name="stack_deployment_steps"
    )
    op.drop_table("stack_deployment_steps")
    op.drop_index(op.f("ix_stack_deployments_stack_id"), table_name="stack_deployments")
    op.drop_table("stack_deployments")
    op.drop_constraint(
        op.f("fk_repository_analyses_stack_id_service_stacks"),
        "repository_analyses",
        type_="foreignkey",
    )
    op.drop_index("uq_repository_analyses_stack_id_source_sha", table_name="repository_analyses")
    op.drop_column("repository_analyses", "stack_id")
    op.drop_constraint(op.f("fk_services_stack_id_service_stacks"), "services", type_="foreignkey")
    op.drop_index("uq_services_stack_id_stack_unit_id", table_name="services")
    op.drop_index(op.f("ix_services_stack_id"), table_name="services")
    op.drop_column("services", "stack_unit_id")
    op.drop_column("services", "stack_id")
    op.drop_index("ix_service_stacks_repository", table_name="service_stacks")
    op.drop_index(op.f("ix_service_stacks_project_id"), table_name="service_stacks")
    op.drop_table("service_stacks")
