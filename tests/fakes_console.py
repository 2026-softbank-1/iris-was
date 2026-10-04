"""서비스 콘솔 테스트용 키와 Control API 쪽 인메모리 Repository."""

from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.enums import OnpremServerStatus, TargetKind
from app.models.console_session import ConsoleSession
from app.models.onprem_server import OnpremServer
from app.models.release import Release
from app.models.service import Service
from app.models.target import Target


def generate_ed25519_pem_pair() -> tuple[str, str]:
    """(개인키 PEM, 공개키 PEM)."""
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


# ---- Control API ------------------------------------------------------------------------


class FakeConsoleServiceRepository:
    def __init__(self) -> None:
        self.services: dict[int, Service] = {}
        self.owner_by_service_id: dict[int, int] = {}
        self.target_ids: dict[int, list[int]] = {}

    def add(self, service_id: int, owner_id: int, target_ids: list[int]) -> None:
        service = Service()
        service.id = service_id
        self.services[service_id] = service
        self.owner_by_service_id[service_id] = owner_id
        self.target_ids[service_id] = target_ids

    async def find_by_id_and_owner_id(
        self, service_id: int, owner_id: int, *, for_update: bool = False
    ) -> Service | None:
        if self.owner_by_service_id.get(service_id) != owner_id:
            return None
        return self.services[service_id]

    async def search_target_ids_by_service_ids(
        self, service_ids: list[int]
    ) -> dict[int, list[int]]:
        return {i: self.target_ids.get(i, []) for i in service_ids}


class FakeConsoleTargetRepository:
    def __init__(self) -> None:
        self.targets: dict[int, Target] = {}

    def add(self, target_id: int, kind: TargetKind | str) -> None:
        """kind 는 TargetKind 가 아닌 값도 받는다(지원하지 않는 종류를 시험할 때)."""
        target = Target()
        target.id = target_id
        target.kind = kind  # type: ignore[assignment]
        self.targets[target_id] = target

    async def search_by_ids(self, target_ids: list[int]) -> list[Target]:
        return [self.targets[i] for i in target_ids if i in self.targets]


class FakeConsoleReleaseRepository:
    def __init__(self) -> None:
        self.releases: dict[tuple[int, int], Release] = {}

    def add(self, service_id: int, target_id: int, release_id: int) -> None:
        release = Release()
        release.id = release_id
        self.releases[(service_id, target_id)] = release

    async def find_last_known_good(self, service_id: int, target_id: int) -> Release | None:
        return self.releases.get((service_id, target_id))


class FakeConsoleOnpremServerRepository:
    """사용자가 등록한 서버(타깃에 `owner_id` 가 있는)의 인메모리 저장소. 공용 타깃은 행이 없다."""

    def __init__(self) -> None:
        self.servers_by_target_id: dict[int, OnpremServer] = {}

    def add(
        self,
        target_id: int,
        status: OnpremServerStatus,
        last_seen_at: datetime | None = None,
        server_id: int = 77,
    ) -> OnpremServer:
        server = OnpremServer()
        server.id = server_id
        server.target_id = target_id
        server.status = status
        server.last_seen_at = last_seen_at
        self.servers_by_target_id[target_id] = server
        return server

    async def find_by_target_id(self, target_id: int) -> OnpremServer | None:
        return self.servers_by_target_id.get(target_id)


class FakeConsoleSessionRepository:
    def __init__(self) -> None:
        self.added: list[ConsoleSession] = []

    async def add(self, console_session: ConsoleSession) -> ConsoleSession:
        self.added.append(console_session)
        return console_session
