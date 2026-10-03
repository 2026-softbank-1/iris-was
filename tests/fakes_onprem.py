"""온프레미스 서버 등록 테스트용 인메모리 Repository·Client 와 조립 도우미."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count

from cryptography.fernet import Fernet

from app.clients.aws_clients import EcrPullCredential
from app.core.crypto import VariableCipher
from app.models.base import now_utc
from app.models.onprem_server import OnpremServer
from app.services.onprem_server_service import OnpremBootstrapSettings, OnpremServerService
from tests.fakes import FakeSession
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository, FakeTargetRepository
from tests.sealed_support import make_controller_key

OWNER = 1
OTHER_OWNER = 2
TAILSCALE_AUTH_KEY = "tskey-auth-test"
_CA_KEY, CA_PEM = make_controller_key()
SERVER_KEY, SEALED_SECRETS_CERT = make_controller_key()


class FakeOnpremServerRepository:
    def __init__(self) -> None:
        self.servers: list[OnpremServer] = []
        self._ids = count(1)

    def _active(self) -> list[OnpremServer]:
        return [s for s in self.servers if not s.is_deleted]

    async def find_by_id_and_owner_id(
        self, server_id: int, owner_id: int, *, for_update: bool = False
    ) -> OnpremServer | None:
        return next(
            (s for s in self._active() if s.id == server_id and s.owner_id == owner_id), None
        )

    async def find_by_owner_id_and_name(self, owner_id: int, name: str) -> OnpremServer | None:
        return next((s for s in self._active() if s.owner_id == owner_id and s.name == name), None)

    async def count_active_by_owner_id_for_update(self, owner_id: int) -> int:
        return sum(1 for s in self._active() if s.owner_id == owner_id)

    async def search_by_owner_id(self, owner_id: int) -> list[OnpremServer]:
        return sorted((s for s in self._active() if s.owner_id == owner_id), key=lambda s: -s.id)

    async def find_by_registration_token_hash(self, token_hash: str) -> OnpremServer | None:
        return next((s for s in self._active() if s.registration_token_hash == token_hash), None)

    async def find_by_registration_token_hash_for_update(
        self, token_hash: str
    ) -> OnpremServer | None:
        return await self.find_by_registration_token_hash(token_hash)

    async def find_by_server_secret_hash(self, secret_hash: str) -> OnpremServer | None:
        return next((s for s in self._active() if s.server_secret_hash == secret_hash), None)

    async def save(self, server: OnpremServer) -> OnpremServer:
        if server.id is None:
            server.id = next(self._ids)
            server.created_at = server.updated_at = now_utc()
            server.is_deleted = False
            server.connect_generation = server.connect_generation or 0
            server.gitops_attempts = server.gitops_attempts or 0
            self.servers.append(server)
        return server


@dataclass
class FakeEcrPullClient:
    registry: str = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    async def issue_pull_credential(
        self, session_name: str, repository_names: list[str]
    ) -> EcrPullCredential:
        self.calls.append((session_name, repository_names))
        return EcrPullCredential(
            self.registry, "AWS", "ecr-password", datetime.now(UTC) + timedelta(hours=12)
        )


class OnpremSetup:
    """사용자 1(OWNER)과 서버 등록에 필요한 가짜들을 묶는다."""

    def __init__(
        self,
        *,
        tailscale_auth_key: str | None = TAILSCALE_AUTH_KEY,
        has_cipher: bool = True,
        has_ecr: bool = True,
    ) -> None:
        self.session = FakeSession()
        self.servers = FakeOnpremServerRepository()
        self.targets = FakeTargetRepository()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.cipher = VariableCipher(Fernet.generate_key().decode())
        self.ecr = FakeEcrPullClient()
        self.service = OnpremServerService(
            self.session,  # type: ignore[arg-type]
            self.servers,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            OnpremBootstrapSettings(
                tailscale_auth_key=tailscale_auth_key,
                k3s_version="v1.33.13+k3s2",
                argo_rollouts_version="v1.10.0",
                sealed_secrets_version="0.40.0",
            ),
            cipher=self.cipher if has_cipher else None,
            ecr_pull_client=self.ecr if has_ecr else None,  # type: ignore[arg-type]
        )

    async def connect(self, token: str, server: OnpremServer) -> str:
        connection = await self.service.connect(
            token,
            tailnet_fqdn=f"iris-{server.server_key}.tailb046e8.ts.net",
            api_ca_cert=CA_PEM,
            service_account_token="sa-token",
            sealed_secrets_cert=SEALED_SECRETS_CERT,
        )
        return connection.server_secret
