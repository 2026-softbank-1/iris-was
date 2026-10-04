"""온프레미스 서버 등록 테스트용 인메모리 Repository·Client 와 조립 도우미."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count

from cryptography.fernet import Fernet

from app.clients.aws_clients import EcrPullCredential
from app.core.crypto import VariableCipher
from app.models.base import now_utc
from app.models.onprem_metric_sample import OnpremMetricSample
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

# 서버 이름 규칙(앞뒤 공백을 자른 뒤): 1~63자, 영문·숫자·한글 완성형·`.`·`_`·`-` 만,
# 첫 글자는 영문·숫자·한글, 숫자만으로는 안 된다(CLI 의 `<이름|id>` 가 숫자를 id 로 먼저 읽는다).
# 규칙 이전에 등록한 이름이라 규칙에 어긋나지만 그대로 쓰는 이름
LEGACY_SERVER_NAMES = ["E2E Dup 2!", "2024"]
NAME_BLANK = "must not be blank"
NAME_TOO_LONG = "must be at most 63 characters"
NAME_ONLY_DIGITS = "must not be only digits"
NAME_BAD_FIRST = "must start with a letter, digit or Hangul syllable"
NAME_BAD_CHAR = "may contain only letters, digits, Hangul syllables, '.', '_' and '-' (no spaces)"

# 받는 이름과 자른 뒤의 이름
VALID_SERVER_NAMES: list[tuple[str, str]] = [
    ("home-lab", "home-lab"),
    ("Home_Lab.01", "Home_Lab.01"),
    ("a", "a"),
    ("0a", "0a"),
    ("1a", "1a"),
    ("a1", "a1"),
    ("1-2", "1-2"),
    ("1.5", "1.5"),
    ("007a", "007a"),
    ("1_0", "1_0"),
    ("12가", "12가"),
    ("0-" + "0" * 61, "0-" + "0" * 61),
    ("1" * 62 + "a", "1" * 62 + "a"),
    (" 1a ", "1a"),
    ("가", "가"),
    ("서버1", "서버1"),
    ("홈랩-서버_01.a", "홈랩-서버_01.a"),
    ("a.b", "a.b"),
    ("a-", "a-"),
    ("a" + "-" * 62, "a" + "-" * 62),
    ("a" * 63, "a" * 63),
    ("서" * 63, "서" * 63),
    ("a" + "가" * 62, "a" + "가" * 62),
    (" home-lab ", "home-lab"),
    ("\thome-lab\n", "home-lab"),
    ("\u3000홈랩\u3000", "홈랩"),
    (" " + "a" * 63 + " ", "a" * 63),
]

# 받을 수 없는 이름과 응답 `details[0].reason`
INVALID_SERVER_NAMES: list[tuple[str, str]] = [
    ("", NAME_BLANK),
    ("   ", NAME_BLANK),
    ("\t\n", NAME_BLANK),
    ("\u3000", NAME_BLANK),
    ("a" * 64, NAME_TOO_LONG),
    ("서" * 64, NAME_TOO_LONG),
    (" " + "a" * 64 + " ", NAME_TOO_LONG),
    ("1", NAME_ONLY_DIGITS),
    ("0", NAME_ONLY_DIGITS),
    ("007", NAME_ONLY_DIGITS),
    ("2024", NAME_ONLY_DIGITS),
    ("1" * 63, NAME_ONLY_DIGITS),
    (" 12 ", NAME_ONLY_DIGITS),
    ("\t7\n", NAME_ONLY_DIGITS),
    ("1" * 64, NAME_TOO_LONG),
    ("1 2", NAME_BAD_CHAR),
    ("home lab", NAME_BAD_CHAR),
    ("홈 랩", NAME_BAD_CHAR),
    ("E2E Dup 2!", NAME_BAD_CHAR),
    ("home\tlab", NAME_BAD_CHAR),
    ("home\nlab", NAME_BAD_CHAR),
    ("a\u00a0b", NAME_BAD_CHAR),
    ("a\u200bb", NAME_BAD_CHAR),
    ("name!", NAME_BAD_CHAR),
    ("a@b", NAME_BAD_CHAR),
    ("a/b", NAME_BAD_CHAR),
    ("a\\b", NAME_BAD_CHAR),
    ("a:b", NAME_BAD_CHAR),
    ("a,b", NAME_BAD_CHAR),
    ("a+b", NAME_BAD_CHAR),
    ("서버🙂", NAME_BAD_CHAR),
    ("🙂서버", NAME_BAD_FIRST),
    ("aㄱ", NAME_BAD_CHAR),
    ("ㄱabc", NAME_BAD_FIRST),
    ("ㅏ", NAME_BAD_FIRST),
    # 한글 자모를 풀어 쓴 형태(NFD)는 완성형이 아니다
    ("\u1112\u1161\u11ab", NAME_BAD_FIRST),
    ("-abc", NAME_BAD_FIRST),
    (".abc", NAME_BAD_FIRST),
    ("_abc", NAME_BAD_FIRST),
    ("-", NAME_BAD_FIRST),
    (" -abc ", NAME_BAD_FIRST),
    ("ａｂｃ", NAME_BAD_FIRST),
    ("١٢٣", NAME_BAD_FIRST),
]


class FakeOnpremServerRepository:
    def __init__(self) -> None:
        self.servers: list[OnpremServer] = []
        self.touches = 0
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

    async def touch_last_seen(self, server_id: int, now: datetime, min_interval: timedelta) -> None:
        self.touches += 1
        server = next(s for s in self.servers if s.id == server_id)
        if server.last_seen_at is None or server.last_seen_at <= now - min_interval:
            server.last_seen_at = now

    async def save(self, server: OnpremServer) -> OnpremServer:
        if server.id is None:
            server.id = next(self._ids)
            server.created_at = server.updated_at = now_utc()
            server.is_deleted = False
            server.connect_generation = server.connect_generation or 0
            server.gitops_attempts = server.gitops_attempts or 0
            self.servers.append(server)
        return server


def name_ids(cases: list[tuple[str, str]]) -> list[str]:
    """긴 이름이 테스트 ID 를 어지럽히지 않게 앞부분과 길이만 쓴다."""
    return [f"{name[:16]!r}-len{len(name)}" for name, _ in cases]


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


class FakeOnpremMetricSampleRepository:
    def __init__(self) -> None:
        self.samples: list[OnpremMetricSample] = []
        self.deleted_before: list[datetime] = []

    async def add_all(self, samples: list[OnpremMetricSample]) -> None:
        self.samples.extend(samples)

    async def search_by_service_id(
        self, service_id: int, start: datetime, end: datetime
    ) -> list[OnpremMetricSample]:
        return sorted(
            (
                s
                for s in self.samples
                if s.service_id == service_id and start <= s.collected_at <= end
            ),
            key=lambda s: s.collected_at,
        )

    async def delete_collected_before(self, cutoff: datetime) -> None:
        self.deleted_before.append(cutoff)
        self.samples = [s for s in self.samples if s.collected_at >= cutoff]


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
        self.metrics = FakeOnpremMetricSampleRepository()
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
            metric_sample_repository=self.metrics,  # type: ignore[arg-type]
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
