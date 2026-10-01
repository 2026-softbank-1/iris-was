"""add auth project service tables

Revision ID: da3066d208c9
Revises:
Create Date: 2026-10-01 02:31:25.477586

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "da3066d208c9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "github_installations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_installations")),
        sa.UniqueConstraint(
            "installation_id", name=op.f("uq_github_installations_installation_id")
        ),
    )
    op.create_table(
        "targets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "AWS",
                "LOCAL",
                name="target_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("region", sa.String(length=32), nullable=True),
        sa.Column("domain_suffix", sa.String(length=255), nullable=True),
        sa.Column("cluster_ref", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint("kind IN ('AWS', 'LOCAL')", name=op.f("ck_targets_target_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_targets")),
        sa.UniqueConstraint("name", name=op.f("uq_targets_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("github_id", name=op.f("uq_users_github_id")),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
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
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_projects_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_index(op.f("ix_projects_owner_id"), "projects", ["owner_id"], unique=False)
    op.create_index(
        "uq_projects_owner_id_name",
        "projects",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )
    op.create_table(
        "user_github_installations",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["github_installation_id"],
            ["github_installations.id"],
            name=op.f("fk_user_github_installations_github_installation_id_github_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_github_installations_user_id_users")
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "github_installation_id", name=op.f("pk_user_github_installations")
        ),
    )
    op.create_table(
        "services",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source_repository_url", sa.String(length=500), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("root_directory", sa.String(length=255), nullable=True),
        sa.Column("is_auto_deploy", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "builder",
            sa.Enum(
                "dockerfile",
                "railpack",
                name="builder",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("dockerfile_path", sa.String(length=255), nullable=True),
        sa.Column("platform", sa.String(length=32), server_default="linux/amd64", nullable=False),
        sa.Column("railpack_version", sa.String(length=32), nullable=True),
        sa.Column("analysis_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("build_command", sa.Text(), nullable=True),
        sa.Column("start_command", sa.Text(), nullable=True),
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
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "builder IN ('dockerfile', 'railpack')", name=op.f("ck_services_builder")
        ),
        sa.ForeignKeyConstraint(
            ["github_installation_id"],
            ["github_installations.id"],
            name=op.f("fk_services_github_installation_id_github_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_services_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
    )
    op.create_index(
        op.f("ix_services_github_installation_id"),
        "services",
        ["github_installation_id"],
        unique=False,
    )
    op.create_index(op.f("ix_services_project_id"), "services", ["project_id"], unique=False)
    op.create_index(
        "uq_services_project_id_name",
        "services",
        ["project_id", "name"],
        unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )
    op.create_table(
        "service_targets",
        sa.Column("service_id", sa.BigInteger(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_service_targets_service_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["targets.id"], name=op.f("fk_service_targets_target_id_targets")
        ),
        sa.PrimaryKeyConstraint("service_id", "target_id", name=op.f("pk_service_targets")),
    )
    op.create_index(
        op.f("ix_service_targets_target_id"), "service_targets", ["target_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_service_targets_target_id"), table_name="service_targets")
    op.drop_table("service_targets")
    op.drop_index(
        "uq_services_project_id_name",
        table_name="services",
        postgresql_where=sa.text("NOT is_deleted"),
    )
    op.drop_index(op.f("ix_services_project_id"), table_name="services")
    op.drop_index(op.f("ix_services_github_installation_id"), table_name="services")
    op.drop_table("services")
    op.drop_table("user_github_installations")
    op.drop_index(
        "uq_projects_owner_id_name",
        table_name="projects",
        postgresql_where=sa.text("NOT is_deleted"),
    )
    op.drop_index(op.f("ix_projects_owner_id"), table_name="projects")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("targets")
    op.drop_table("github_installations")
