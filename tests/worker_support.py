"""Build·Deploy Worker 통합 테스트가 함께 쓰는 데이터 정리·시드 도우미.

`alembic upgrade head` 가 끝난 TEST_DATABASE_URL 의 PostgreSQL 이 필요하다. 스키마는 건드리지 않고
테스트 데이터 테이블만 비운다(`targets` 의 공용 타깃은 마이그레이션이 넣은 그대로 둔다). Worker 는
여러 트랜잭션을 실제로 커밋하므로 롤백 대신 이 도우미로 정리한다. 개발 DB 에는 쓰지 않는다.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import GithubInstallation, Project, Service, User

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_database = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

# users 는 TRUNCATE 하지 않는다. targets 가 users 를 참조해 CASCADE 가 공용 타깃까지 비운다.
_DATA_TABLES = (
    "onprem_servers, jobs, releases, builds, deployment_status_histories, deployment_requests, "
    "service_uploads, service_targets, services, projects, user_github_installations, "
    "github_installations, cli_login_sessions"
)


async def session_factory_with_clean_data() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """테스트 전후로 데이터 테이블을 비우는 session factory. pytest fixture 안에서 쓴다."""
    engine = create_async_engine(TEST_DATABASE_URL or "")
    await _truncate(engine)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await _truncate(engine)
    await engine.dispose()


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {_DATA_TABLES} RESTART IDENTITY CASCADE"))
        # 사용자가 등록한 서버의 타깃만 지우고 마이그레이션이 넣은 공용 타깃은 남긴다.
        await connection.execute(text("DELETE FROM targets WHERE owner_id IS NOT NULL"))
        await connection.execute(text("DELETE FROM users"))
        await connection.execute(text("ALTER TABLE users ALTER COLUMN id RESTART WITH 1"))


async def add[T](session: AsyncSession, instance: T) -> T:
    session.add(instance)
    await session.flush()
    return instance


async def seed_service(session: AsyncSession, owner_github_id: int = 1) -> Service:
    """사용자·GitHub 설치·프로젝트·서비스를 만든다. 같은 `owner_github_id` 면 사용자를 공유한다."""
    user = await session.scalar(select(User).where(User.github_id == owner_github_id))
    if user is None:
        user = await add(session, User(github_id=owner_github_id, login=f"user{owner_github_id}"))
    installation = await session.scalar(
        select(GithubInstallation).where(GithubInstallation.installation_id == 700001)
    )
    if installation is None:
        installation = await add(
            session,
            GithubInstallation(installation_id=700001, account_login="owner", account_type="User"),
        )
    project = await add(session, Project(name=f"p-{uuid4().hex[:8]}", owner_id=user.id))
    return await add(
        session,
        Service(
            project_id=project.id,
            name="web",
            source_repository_url="https://github.com/owner/repo",
            github_installation_id=installation.id,
            source_branch="main",
        ),
    )
