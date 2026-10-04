from datetime import UTC, datetime, timedelta

from app.core.config import DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS
from app.enums import OnpremServerConnectionStatus
from app.models.target import Target
from app.repositories.target_repository import TargetRepository


class TargetService:
    """배포 대상(타깃) 조회. 공용 타깃은 시드 데이터이고, 서버 타깃은 서버를 등록할 때 생긴다."""

    def __init__(
        self,
        target_repository: TargetRepository,
        *,
        offline_after: timedelta = timedelta(seconds=DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS),
    ) -> None:
        self._target_repository = target_repository
        self._offline_after = offline_after

    def connection_status(self, target: Target) -> OnpremServerConnectionStatus | None:
        """서버 타깃의 연결 상태(하트비트가 끊기면 DISCONNECTED). 공용 타깃은 None 이다."""
        if target.onprem_server is None:
            return None
        return target.onprem_server.connection_status(datetime.now(UTC), self._offline_after)

    async def search_targets(self, owner_id: int) -> list[Target]:
        """공용 타깃과 이 사용자가 등록한 서버의 타깃. 서버(`onprem_server`)를 함께 읽는다."""
        return await self._target_repository.search_visible(owner_id)
