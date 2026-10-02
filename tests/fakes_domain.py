"""서비스 도메인 조회 테스트용 인메모리 Repository."""

from app.models.release import Release


class FakeReleaseRepository:
    """`find_last_known_good` 만 흉내 낸다. `connected` 에 넣은 (service_id, target_id) 는 성공."""

    def __init__(self) -> None:
        self.connected: set[tuple[int, int]] = set()

    async def find_last_known_good(self, service_id: int, target_id: int) -> Release | None:
        return Release() if (service_id, target_id) in self.connected else None
