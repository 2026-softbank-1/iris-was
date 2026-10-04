"""add database init scripts

관리형 DB 초기화 스크립트(`/docker-entrypoint-initdb.d`) 내용을 sha256 으로 저장하는
database_init_scripts 를 만든다. Build Worker 가 분석할 때 풀어 둔 소스에서 읽어 해시를 다시
확인한 뒤 넣고(같은 내용은 한 번만), 분석 결과(`dependencies[].initScripts`)와 DB 서비스
(`services.database_config.initScripts`)는 sha256 으로 가리킨다. 내용은 바이너리 그대로(bytea)
두며 ConfigMap 한도(1 MiB)를 넘는 행은 CHECK 가 막는다.

Revision ID: 663b24ad4296
Revises: c4e2a7b81f30
Create Date: 2026-10-04 10:17:41.916711

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "663b24ad4296"
down_revision: str | Sequence[str] | None = "c4e2a7b81f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "database_init_scripts",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(content) = size_bytes",
            name=op.f("ck_database_init_scripts_content_size"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 1048576",
            name=op.f("ck_database_init_scripts_size_bytes"),
        ),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_database_init_scripts")),
    )


def downgrade() -> None:
    op.drop_table("database_init_scripts")
