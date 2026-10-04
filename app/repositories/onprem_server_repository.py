from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, OnpremServerNameConflictError
from app.models.onprem_server import ONPREM_SERVER_NAME_INDEX, OnpremServer
from app.models.user import User


def _find_violated_constraint_name(exc: IntegrityError) -> str | None:
    """위반한 제약(인덱스) 이름. 드라이버 예외의 `constraint_name` 에서 읽는다.

    SQLAlchemy 가 드라이버 예외를 한 겹 감싼다. 2.1 은 `.orig`, 2.0 은 `__cause__` 로 이어진다.
    """
    error: BaseException | None = exc.orig
    while error is not None:
        constraint_name = getattr(error, "constraint_name", None)
        if constraint_name:
            return str(constraint_name)
        error = getattr(error, "orig", None) or error.__cause__
    return None


class OnpremServerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id_and_owner_id(
        self, server_id: int, owner_id: int, *, for_update: bool = False
    ) -> OnpremServer | None:
        """소유자 기준으로 서버를 찾는다. 삭제된 서버는 없는 것으로 본다."""
        stmt = select(OnpremServer).where(
            OnpremServer.id == server_id,
            OnpremServer.owner_id == owner_id,
            OnpremServer.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_target_id(self, target_id: int) -> OnpremServer | None:
        """타깃이 사용자가 등록한 서버의 전용 타깃이면 그 서버. 공용 타깃이면 None 이다."""
        stmt = select(OnpremServer).where(
            OnpremServer.target_id == target_id,
            OnpremServer.is_deleted.is_(False),
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_owner_id_and_name(self, owner_id: int, name: str) -> OnpremServer | None:
        stmt = select(OnpremServer).where(
            OnpremServer.owner_id == owner_id,
            OnpremServer.name == name,
            OnpremServer.is_deleted.is_(False),
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def count_active_by_owner_id_for_update(self, owner_id: int) -> int:
        """삭제되지 않은 서버 수. 사용자 행을 잠가 동시 등록이 한도를 넘지 않게 한다."""
        await self._session.execute(select(User.id).where(User.id == owner_id).with_for_update())
        count = await self._session.scalar(
            select(func.count())
            .select_from(OnpremServer)
            .where(OnpremServer.owner_id == owner_id, OnpremServer.is_deleted.is_(False))
        )
        return int(count or 0)

    async def search_by_owner_id(self, owner_id: int) -> list[OnpremServer]:
        """최신 등록 순."""
        stmt = (
            select(OnpremServer)
            .where(OnpremServer.owner_id == owner_id, OnpremServer.is_deleted.is_(False))
            .order_by(OnpremServer.id.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def find_by_registration_token_hash_for_update(
        self, token_hash: str
    ) -> OnpremServer | None:
        """행을 잠가 읽는다. 같은 토큰의 connect 가 동시에 와도 하나씩 덮어쓴다."""
        stmt = (
            select(OnpremServer)
            .where(
                OnpremServer.registration_token_hash == token_hash,
                OnpremServer.is_deleted.is_(False),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_registration_token_hash(self, token_hash: str) -> OnpremServer | None:
        stmt = select(OnpremServer).where(
            OnpremServer.registration_token_hash == token_hash,
            OnpremServer.is_deleted.is_(False),
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def find_by_server_secret_hash(self, secret_hash: str) -> OnpremServer | None:
        stmt = select(OnpremServer).where(
            OnpremServer.server_secret_hash == secret_hash,
            OnpremServer.is_deleted.is_(False),
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def touch_last_seen(self, server_id: int, now: datetime, min_interval: timedelta) -> None:
        """하트비트 시각을 남긴다. min_interval 안에 이미 남겼으면 쓰지 않는다(쓰기 줄이기).

        행을 잠그지 않는 한 문장이라 Worker 의 lease 처리와 겹쳐도 기다리지 않는다.
        """
        await self._session.execute(
            update(OnpremServer)
            .where(
                OnpremServer.id == server_id,
                or_(
                    OnpremServer.last_seen_at.is_(None),
                    OnpremServer.last_seen_at <= now - min_interval,
                ),
            )
            .values(last_seen_at=now)
        )

    async def save(self, server: OnpremServer) -> OnpremServer:
        """같은 소유자·이름(삭제되지 않은 것)이 동시에 들어와 이름 유일 인덱스를 어기면
        OnpremServerNameConflictError. 다른 제약(server_key·target_id 등)을 어긴 IntegrityError 는
        이름 충돌이 아니므로 그대로 올린다. 어느 쪽이든 flush 가 실패했으니 그 뒤 트랜잭션은
        rollback 해야 한다.
        """
        self._session.add(server)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if _find_violated_constraint_name(exc) != ONPREM_SERVER_NAME_INDEX:
                raise
            raise OnpremServerNameConflictError(
                "onprem server name already exists", owner_id=server.owner_id
            ) from exc
        return server

    # --- Deploy Worker

    async def claim_next_due(self, worker_id: str, lease: timedelta) -> OnpremServer | None:
        """next_check_at 이 지난 서버 1대의 lease 를 잡는다. 만료된 lease 도 다시 가져간다."""
        now = func.now()
        next_server_id = (
            select(OnpremServer.id)
            .where(
                OnpremServer.next_check_at <= now,
                or_(OnpremServer.locked_until.is_(None), OnpremServer.locked_until < now),
            )
            .order_by(OnpremServer.next_check_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(OnpremServer)
            .where(OnpremServer.id == next_server_id)
            .values(locked_by=worker_id, locked_until=now + lease)
            .returning(OnpremServer)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def get_by_id_for_update(self, server_id: int) -> OnpremServer:
        stmt = (
            select(OnpremServer)
            .where(OnpremServer.id == server_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        server = (await self._session.scalars(stmt)).one_or_none()
        if server is None:
            raise NotFoundError("onprem server not found", onprem_server_id=server_id)
        return server

    async def find_seconds_until_next_check(self) -> float | None:
        """가장 이른 미래 next_check_at 까지 남은 초. DB 시계로 계산한다."""
        seconds = await self._session.scalar(
            select(func.extract("epoch", func.min(OnpremServer.next_check_at) - func.now())).where(
                OnpremServer.next_check_at > func.now()
            )
        )
        return None if seconds is None else float(seconds)
