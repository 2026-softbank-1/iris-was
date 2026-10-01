from app.models.target import Target
from app.repositories.target_repository import TargetRepository


class TargetService:
    """배포 대상(타깃) 조회. 타깃은 시드 데이터라 읽기만 한다."""

    def __init__(self, target_repository: TargetRepository) -> None:
        self._target_repository = target_repository

    async def search_targets(self) -> list[Target]:
        return await self._target_repository.search_all()
