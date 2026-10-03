"""CLI 로그인 세션을 실제 PostgreSQL 에서 검증한다. TEST_DATABASE_URL 이 없으면 건너뛴다.

대상 DB 는 `alembic upgrade head` 가 끝난 상태여야 한다. 대부분의 테스트는 트랜잭션을 롤백한다.
동시 폴링 테스트만 실제로 커밋하고, 끝나면 만든 행을 직접 지운다.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.enums import CliLoginSessionStatus
from app.models.cli_login_session import CliLoginSession
from app.models.user import User
from app.repositories.cli_login_session_repository import CliLoginSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.cli_login_service import CliLoginService, CliLoginStart
from app.services.session_service import SessionService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

SECRET = "test-secret-with-enough-length-for-hs256"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(connection, expire_on_commit=False)
        async with factory() as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


def _build_service(session: AsyncSession) -> CliLoginService:
    users = UserRepository(session)
    return CliLoginService(
        session,
        CliLoginSessionRepository(session),
        users,
        SessionService(users, SECRET, timedelta(minutes=5)),
    )


def _make_cli_session(public_id: str, expires_in: timedelta) -> CliLoginSession:
    return CliLoginSession(
        public_id=public_id,
        poll_secret_hash="0" * 64,
        status=CliLoginSessionStatus.PENDING,
        expires_at=datetime.now(UTC) + expires_in,
    )


async def test_cli_login_session_round_trips_with_defaults(session: AsyncSession) -> None:
    repository = CliLoginSessionRepository(session)

    saved = await repository.save(_make_cli_session("it-public-id", timedelta(minutes=10)))
    found = await repository.find_by_public_id("it-public-id")

    assert found is saved
    assert saved.id is not None
    assert saved.status == CliLoginSessionStatus.PENDING
    assert saved.user_id is None
    assert saved.consumed_at is None
    assert saved.last_polled_at is None
    assert saved.created_at is not None
    assert await repository.find_by_public_id("other") is None


async def test_public_id_is_unique(session: AsyncSession) -> None:
    repository = CliLoginSessionRepository(session)
    await repository.save(_make_cli_session("it-dup", timedelta(minutes=10)))

    with pytest.raises(IntegrityError):
        await repository.save(_make_cli_session("it-dup", timedelta(minutes=10)))


async def test_status_outside_the_enum_is_rejected_by_check_constraint(
    session: AsyncSession,
) -> None:
    saved = await CliLoginSessionRepository(session).save(
        _make_cli_session("it-check", timedelta(minutes=10))
    )

    with pytest.raises(IntegrityError, match="cli_login_session_status"):
        await session.execute(
            text("UPDATE cli_login_sessions SET status = 'BOGUS' WHERE id = :id"), {"id": saved.id}
        )


async def test_delete_expired_before_removes_only_older_rows(session: AsyncSession) -> None:
    repository = CliLoginSessionRepository(session)
    await repository.save(_make_cli_session("it-old", -timedelta(days=2)))
    await repository.save(_make_cli_session("it-recent", -timedelta(minutes=1)))
    await repository.save(_make_cli_session("it-live", timedelta(minutes=5)))

    await repository.delete_expired_before(datetime.now(UTC) - timedelta(days=1))

    assert await repository.find_by_public_id("it-old") is None
    assert await repository.find_by_public_id("it-recent") is not None
    assert await repository.find_by_public_id("it-live") is not None


async def test_find_for_update_returns_the_row_with_fresh_values(session: AsyncSession) -> None:
    repository = CliLoginSessionRepository(session)
    saved = await repository.save(_make_cli_session("it-lock", timedelta(minutes=10)))
    await session.execute(
        text("UPDATE cli_login_sessions SET status = 'DENIED' WHERE id = :id"), {"id": saved.id}
    )

    locked = await repository.find_by_public_id_for_update("it-lock")

    assert locked is saved
    assert locked.status == CliLoginSessionStatus.DENIED  # 메모리에 있던 PENDING 이 아니라 DB 값


async def test_login_flow_on_real_database_issues_token_once(session: AsyncSession) -> None:
    user = User(github_id=900100, login="cli-flow-user")
    session.add(user)
    await session.flush()
    service = _build_service(session)
    start = await service.create_session()

    pending = await service.poll_token(start.session_id, start.poll_secret)
    await service.approve_session(start.session_id, user)
    first = await service.poll_token(start.session_id, start.poll_secret)
    second = await service.poll_token(start.session_id, start.poll_secret)

    assert pending.status == CliLoginSessionStatus.PENDING
    assert first.status == CliLoginSessionStatus.APPROVED
    assert first.access_token is not None
    assert second.status == CliLoginSessionStatus.EXPIRED
    assert second.access_token is None
    stored = await CliLoginSessionRepository(session).find_by_public_id(start.session_id)
    assert stored is not None
    assert stored.user_id == user.id
    assert stored.consumed_at is not None


async def test_concurrent_polls_of_approved_session_issue_the_token_only_once() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start: CliLoginStart | None = None
    try:
        async with factory() as setup:
            user = User(github_id=900200, login="cli-concurrent-user")
            setup.add(user)
            await setup.flush()
            service = _build_service(setup)
            start = await service.create_session()
            await service.approve_session(start.session_id, user)
            await setup.commit()
        poll_target = start

        async def poll() -> tuple[CliLoginSessionStatus, str | None]:
            async with factory() as poller:
                result = await _build_service(poller).poll_token(
                    poll_target.session_id, poll_target.poll_secret
                )
                return result.status, result.access_token

        results = await asyncio.gather(*(poll() for _ in range(5)))

        statuses = [status for status, _ in results]
        tokens = [token for _, token in results if token is not None]
        assert statuses.count(CliLoginSessionStatus.APPROVED) == 1
        assert statuses.count(CliLoginSessionStatus.EXPIRED) == 4
        assert len(tokens) == 1
    finally:
        async with factory() as cleanup:
            if start is not None:
                await cleanup.execute(
                    delete(CliLoginSession).where(CliLoginSession.public_id == start.session_id)
                )
            await cleanup.execute(delete(User).where(User.github_id == 900200))
            await cleanup.commit()
        await engine.dispose()
