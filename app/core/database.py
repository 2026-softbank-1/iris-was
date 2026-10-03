from functools import lru_cache

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    # commit 뒤에도 모델 속성을 읽어 응답으로 변환하므로 만료시키지 않는다.
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def connect_listener() -> asyncpg.Connection:
    """LISTEN 용 asyncpg 연결. 풀 연결은 반납되므로 LISTEN 을 붙잡아 둘 수 없어 따로 연다.

    TLS 는 엔진과 같이 asyncpg 가 PGSSLMODE·PGSSLROOTCERT 환경변수로 맞춘다. URL 의 query 는
    SQLAlchemy dialect 옵션이라 asyncpg 에 넘기면 서버 설정으로 오인되므로 뺀다. 연결이 막혀도
    종료 신호를 오래 막지 않게 timeout 을 짧게 둔다.
    """
    url = make_url(get_settings().database_url).set(drivername="postgresql", query={})
    return await asyncpg.connect(url.render_as_string(hide_password=False), timeout=5)
