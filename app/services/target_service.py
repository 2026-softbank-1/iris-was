from app.models.target import Target
from app.repositories.target_repository import TargetRepository


class TargetService:
    """배포 대상(타깃) 조회. 공용 타깃은 시드 데이터이고, 서버 타깃은 서버를 등록할 때 생긴다."""

    def __init__(self, target_repository: TargetRepository) -> None:
        self._target_repository = target_repository

    async def search_targets(self, owner_id: int) -> list[Target]:
        """공용 타깃과 이 사용자가 등록한 서버의 타깃. 서버(`onprem_server`)를 함께 읽는다."""
        return await self._target_repository.search_visible(owner_id)
