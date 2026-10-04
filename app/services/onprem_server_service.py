"""사용자가 직접 등록하는 온프레미스 서버(ADR 0029). Control API 가 쓴다.

등록하면 서버 전용 타깃과 1회용 등록 토큰이 생긴다. 서버의 설치 스크립트가 토큰으로 설정값을 받고
(bootstrap), 클러스터 접속 정보를 보내면(connect) Deploy Worker 가 GitOps 에 반영해 연결을 확인한다.
Control API 는 GitHub·Argo CD 를 부르지 않는다.
"""

import asyncio
import logging
import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.aws_clients import EcrPullCredentialClient
from app.clients.secret_sealer import SecretSealer
from app.core.config import DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS
from app.core.crypto import VariableCipher
from app.core.exceptions import (
    ExternalError,
    FieldIssue,
    InvalidInputError,
    InvalidRegistrationTokenError,
    InvalidStatusTransitionError,
    NotConfiguredError,
    OnpremServerInUseError,
    OnpremServerLimitExceededError,
    OnpremServerNameConflictError,
    OnpremServerNotConnectedError,
    OnpremServerNotFoundError,
    UnauthorizedError,
)
from app.core.security import generate_url_token, hash_url_token, verify_url_token
from app.enums import OnpremServerConnectionStatus, OnpremServerStatus, TargetKind
from app.models.onprem_server import OnpremServer
from app.models.target import Target
from app.repositories.onprem_server_repository import OnpremServerRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository

logger = logging.getLogger(__name__)

REGISTRATION_TOKEN_TTL = timedelta(hours=24)
# 하트비트(last_seen_at)를 이보다 자주 쓰지 않는다. 서버는 1분마다 부른다.
HEARTBEAT_WRITE_INTERVAL = timedelta(seconds=30)
ONPREM_DOMAIN_SUFFIX = "internal.likelion.uk"
TAILSCALE_TAGS = ("tag:iris-onprem",)
INSTALL_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "assets" / "onprem" / "install.sh"
# 첫 글자 영문 + 영문·숫자 7자. host 의 마지막 `-` 뒤가 이 모양이면 게이트웨이가 서버로 보낸다.
_SERVER_KEY_FIRST = string.ascii_lowercase
_SERVER_KEY_REST = string.ascii_lowercase + string.digits
_SERVER_KEY_LENGTH = 8
# REGISTERING 에서도 다시 받는다. 잘못된 서버에서 실행했거나 연결이 멈췄을 때 처음부터 한다.
_REISSUABLE_STATUSES = (
    OnpremServerStatus.PENDING,
    OnpremServerStatus.REGISTERING,
    OnpremServerStatus.FAILED,
)
# 사용자마다 등록할 수 있는 서버 수(삭제한 것 제외). 운영자 Tailscale 키를 같이 써서 묶어 둔다.
MAX_SERVERS_PER_OWNER = 5
# connect 뒤 스크립트가 중간에 실패해도 같은 명령으로 다시 돌릴 수 있게 REGISTERING 도 받는다.
# bootstrap 은 상태를 바꾸지 않고, connect 는 같은 토큰으로 다시 보내도 덮어쓴다.
_RERUNNABLE_STATUSES = (
    OnpremServerStatus.PENDING,
    OnpremServerStatus.REGISTERING,
    OnpremServerStatus.FAILED,
)
# iris-infra chart `iris-onprem-server` 의 values schema 가 받는 tailnet FQDN 모양과 같다.
_TAILNET_SUFFIX = re.compile(r"\.[a-z0-9-]+\.ts\.net")
# 서버 이름(앞뒤 공백을 자른 뒤): 1~63자, 영문 대소문자·숫자·한글 완성형(가-힣)·`.`·`_`·`-` 만 쓰고
# 첫 글자는 영문·숫자·한글이며 숫자만으로는 안 된다. 이름이 CLI 인자·화면·로그에 그대로 쓰여, 공백·
# 특수문자가 있으면 따옴표가 필요하고 표시가 깨진다. 숫자만인 이름은 CLI `<이름|id>` 가 숫자를 id 로
# 먼저 읽어 다른 서버를 가리킬 수 있다. 대소문자는 구분한다. 규칙은 등록할 때만 본다.
# OpenAPI 의 JSON Schema(ECMA) 패턴으로도 쓰므로 `\Z` 대신 `$` 를 쓴다.
SERVER_NAME_MAX_LENGTH = 63
SERVER_NAME_PATTERN = re.compile(r"(?![0-9]+$)[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]{0,62}")
_SERVER_NAME_DIGITS_ONLY = re.compile(r"[0-9]+")
_SERVER_NAME_FIRST = re.compile(r"[A-Za-z0-9가-힣]")


def generate_server_key() -> str:
    first = secrets.choice(_SERVER_KEY_FIRST)
    rest = "".join(secrets.choice(_SERVER_KEY_REST) for _ in range(_SERVER_KEY_LENGTH - 1))
    return first + rest


def onprem_target_name(server_key: str) -> str:
    """서버 타깃 이름. Argo CD cluster 이름·GitOps 서비스 디렉터리 이름과 같다."""
    return f"onprem-{server_key}"


def tailscale_hostname(server_key: str) -> str:
    return f"iris-{server_key}"


@dataclass(frozen=True)
class OnpremServerRegistration:
    server: OnpremServer
    # 평문은 이 응답에서만 볼 수 있다. DB 에는 해시만 있다.
    registration_token: str


@dataclass(frozen=True)
class OnpremBootstrap:
    server_key: str
    tailscale_auth_key: str
    tailscale_hostname: str
    tailscale_tags: tuple[str, ...]
    k3s_version: str
    argo_rollouts_version: str
    sealed_secrets_version: str


@dataclass(frozen=True)
class OnpremBootstrapSettings:
    """bootstrap 응답에 담는 설정. 가입 키가 없으면 bootstrap 은 503 이다."""

    tailscale_auth_key: str | None
    k3s_version: str
    argo_rollouts_version: str
    sealed_secrets_version: str


@dataclass(frozen=True)
class OnpremConnection:
    status: OnpremServerStatus
    # 평문은 이 응답에서만 볼 수 있다. 서버가 ECR 자격증명을 받을 때 쓴다.
    server_secret: str


@dataclass(frozen=True)
class RegistryCredentials:
    registry: str
    username: str
    # 붙은 서비스가 없으면 자격증명을 만들지 않는다.
    password: str | None
    expires_at: datetime | None
    service_ids: list[int]


async def load_install_script(path: Path) -> bytes:
    """설치 스크립트 원문. 이미지에 파일이 없으면 503 이다."""
    try:
        return await asyncio.to_thread(path.read_bytes)
    except FileNotFoundError as exc:
        raise NotConfiguredError("install script is missing", path=str(path)) from exc


class OnpremServerService:
    def __init__(
        self,
        session: AsyncSession,
        onprem_server_repository: OnpremServerRepository,
        target_repository: TargetRepository,
        service_repository: ServiceRepository,
        bootstrap_settings: OnpremBootstrapSettings,
        *,
        cipher: VariableCipher | None = None,
        ecr_pull_client: EcrPullCredentialClient | None = None,
        offline_after: timedelta = timedelta(seconds=DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS),
    ) -> None:
        self._session = session
        self._onprem_server_repository = onprem_server_repository
        self._target_repository = target_repository
        self._service_repository = service_repository
        self._bootstrap_settings = bootstrap_settings
        # connect 가 ServiceAccount 토큰을 암호화할 때만 쓴다.
        self._cipher = cipher
        # registry-credentials 만 쓴다.
        self._ecr_pull_client = ecr_pull_client
        self._offline_after = offline_after

    def connection_status(self, server: OnpremServer) -> OnpremServerConnectionStatus:
        """API 가 알리는 상태. 하트비트가 끊긴 CONNECTED 서버는 DISCONNECTED 다."""
        return server.connection_status(datetime.now(UTC), self._offline_after)

    # --- 사용자 API

    async def create_server(self, owner_id: int, name: str) -> OnpremServerRegistration:
        """서버와 전용 타깃을 한 트랜잭션에서 만든다. 등록 토큰은 24시간 유효하다.

        이름은 앞뒤 공백을 자른 값으로 규칙을 검사하고 저장하고 중복을 비교한다(규칙을 어기면 422).
        사용자마다 MAX_SERVERS_PER_OWNER 대까지다. 같은 사용자의 등록은 사용자 행 잠금으로
        줄을 세운다.
        """
        name = name.strip()
        _validate_server_name(name, owner_id)
        count = await self._onprem_server_repository.count_active_by_owner_id_for_update(owner_id)
        if count >= MAX_SERVERS_PER_OWNER:
            raise OnpremServerLimitExceededError(
                "onprem server limit exceeded", limit=MAX_SERVERS_PER_OWNER
            )
        if await self._onprem_server_repository.find_by_owner_id_and_name(owner_id, name):
            # fields 는 logging extra 로 넘어가므로 LogRecord 예약 속성인 `name` 을 쓰면 안 된다.
            raise OnpremServerNameConflictError(
                "onprem server name already exists", onprem_server_name=name
            )
        server_key = generate_server_key()
        target = await self._target_repository.add(
            Target(
                name=onprem_target_name(server_key),
                kind=TargetKind.ONPREM,
                domain_suffix=ONPREM_DOMAIN_SUFFIX,
                owner_id=owner_id,
            )
        )
        token = generate_url_token()
        try:
            server = await self._onprem_server_repository.save(
                OnpremServer(
                    owner_id=owner_id,
                    name=name,
                    server_key=server_key,
                    target_id=target.id,
                    status=OnpremServerStatus.PENDING,
                    registration_token_hash=hash_url_token(token),
                    registration_expires_at=datetime.now(UTC) + REGISTRATION_TOKEN_TTL,
                )
            )
        except Exception:
            # flush 가 실패하면 세션 트랜잭션은 더 쓸 수 없다. 앞서 만든 타깃 행과 사용자 행 잠금을
            # 함께 버리고 오류는 그대로 올린다(이름 충돌은 409, 그 밖의 DB 오류는 500).
            await self._session.rollback()
            raise
        await self._session.commit()
        logger.info(
            "onprem server created",
            extra={
                "action": "create_server",
                "onprem_server_id": server.id,
                "target_id": target.id,
            },
        )
        return OnpremServerRegistration(server, token)

    async def search_servers(self, owner_id: int) -> list[OnpremServer]:
        return await self._onprem_server_repository.search_by_owner_id(owner_id)

    async def get_server(self, owner_id: int, server_id: int) -> OnpremServer:
        return await self._get_owned(owner_id, server_id)

    async def reissue_registration_token(
        self, owner_id: int, server_id: int
    ) -> OnpremServerRegistration:
        """PENDING·REGISTERING·FAILED 일 때만. 이전 토큰은 무효가 되고 상태는 PENDING 이다."""
        server = await self._get_owned(owner_id, server_id, for_update=True)
        if server.status not in _REISSUABLE_STATUSES:
            raise InvalidStatusTransitionError(
                "registration token cannot be reissued",
                onprem_server_id=server.id,
                from_status=server.status,
                to_status=OnpremServerStatus.PENDING,
            )
        token = generate_url_token()
        server.reissue_registration_token(
            hash_url_token(token), datetime.now(UTC) + REGISTRATION_TOKEN_TTL
        )
        await self._session.commit()
        logger.info(
            "onprem registration token reissued",
            extra={"action": "reissue_registration_token", "onprem_server_id": server.id},
        )
        return OnpremServerRegistration(server, token)

    async def delete_server(self, owner_id: int, server_id: int) -> None:
        """서버와 타깃을 소프트 삭제한다. Deploy Worker 가 GitOps 의 서버 디렉터리를 지운다."""
        server = await self._get_owned(owner_id, server_id, for_update=True)
        if await self._service_repository.is_target_in_use(server.target_id):
            raise OnpremServerInUseError("onprem server has services", onprem_server_id=server.id)
        targets = await self._target_repository.search_by_ids([server.target_id])
        now = datetime.now(UTC)
        server.remove(now)
        for target in targets:
            target.mark_as_deleted()
        await self._session.commit()
        logger.info(
            "onprem server deleted",
            extra={"action": "delete_server", "onprem_server_id": server.id},
        )

    # --- 서버 API (사용자 인증 없음)

    async def bootstrap(self, registration_token: str) -> OnpremBootstrap:
        """설치 스크립트가 처음 받는 설정. 토큰이 맞지 않으면 사유를 가리지 않고 401 이다."""
        settings = self._bootstrap_settings
        if settings.tailscale_auth_key is None:
            raise NotConfiguredError(
                "tailscale auth key is not configured", setting="ONPREM_TAILSCALE_AUTH_KEY"
            )
        server = await self._onprem_server_repository.find_by_registration_token_hash(
            hash_url_token(registration_token)
        )
        server = _check_registration_token(server, registration_token, _RERUNNABLE_STATUSES)
        await self._record_seen(server)
        logger.info(
            "onprem server bootstrapped",
            extra={"action": "bootstrap", "onprem_server_id": server.id},
        )
        return OnpremBootstrap(
            server_key=server.server_key,
            tailscale_auth_key=settings.tailscale_auth_key,
            tailscale_hostname=tailscale_hostname(server.server_key),
            tailscale_tags=TAILSCALE_TAGS,
            k3s_version=settings.k3s_version,
            argo_rollouts_version=settings.argo_rollouts_version,
            sealed_secrets_version=settings.sealed_secrets_version,
        )

    async def connect(
        self,
        registration_token: str,
        *,
        tailnet_fqdn: str,
        api_ca_cert: str,
        service_account_token: str,
        sealed_secrets_cert: str,
    ) -> OnpremConnection:
        """서버의 클러스터 접속 정보를 저장하고 REGISTERING 으로 바꾼다.

        같은 토큰으로 다시 보내면 값을 덮어쓰고 새 서버 비밀을 준다. Deploy Worker 는 처음부터
        다시 반영한다(connect_generation).
        """
        if self._cipher is None:
            raise NotConfiguredError(
                "variables encryption is not configured", setting="VARIABLES_ENCRYPTION_KEY"
            )
        server = await self._onprem_server_repository.find_by_registration_token_hash_for_update(
            hash_url_token(registration_token)
        )
        server = _check_registration_token(server, registration_token, _RERUNNABLE_STATUSES)
        tailnet_fqdn = tailnet_fqdn.strip().lower().removesuffix(".")
        _validate_connection(server, tailnet_fqdn, api_ca_cert, sealed_secrets_cert)

        server_secret = generate_url_token()
        server.start_registering(
            tailnet_fqdn=tailnet_fqdn,
            api_ca_cert=api_ca_cert.strip() + "\n",
            encrypted_service_account_token=self._cipher.encrypt(service_account_token.strip()),
            sealed_secrets_cert=sealed_secrets_cert.strip() + "\n",
            server_secret_hash=hash_url_token(server_secret),
            now=datetime.now(UTC),
        )
        await self._session.commit()
        logger.info(
            "onprem server connecting",
            extra={
                "action": "connect",
                "onprem_server_id": server.id,
                "connect_generation": server.connect_generation,
            },
        )
        return OnpremConnection(server.status, server_secret)

    async def issue_registry_credentials(self, server_secret: str) -> RegistryCredentials:
        """이 서버 타깃에 붙은 서비스들의 ECR 저장소만 받을 수 있는 pull 자격증명.

        비밀이 틀리면 401, 맞지만 아직 CONNECTED 가 아니면 409(서버의 CronJob 이 다음 회차를
        기다린다), 그다음 ECR pull Role 설정이 없으면 503 이다.
        """
        server = await self._onprem_server_repository.find_by_server_secret_hash(
            hash_url_token(server_secret)
        )
        if (
            server is None
            or server.server_secret_hash is None
            or not verify_url_token(server_secret, server.server_secret_hash)
        ):
            raise UnauthorizedError("invalid server secret")
        # 인증이 끝나면 연결 확인 전(409)·설정 없음(503)이어도 서버가 살아 있다는 하트비트로 남긴다.
        await self._record_seen(server)
        if server.status != OnpremServerStatus.CONNECTED:
            raise OnpremServerNotConnectedError(
                "onprem server is not connected",
                onprem_server_id=server.id,
                onprem_server_status=server.status,
            )
        # 설정 확인은 인증 뒤에 한다. 틀리거나 무효가 된 비밀에는 설정 상태를 알리지 않는다.
        if self._ecr_pull_client is None:
            raise NotConfiguredError(
                "ecr pull role is not configured",
                setting="AWS_REGION, ONPREM_ECR_PULL_ROLE_ARN",
            )

        service_ids = await self._service_repository.search_ids_by_target_id(server.target_id)
        registry = self._ecr_pull_client.registry
        if not service_ids:
            return RegistryCredentials(registry, "AWS", None, None, [])
        try:
            credential = await self._ecr_pull_client.issue_pull_credential(
                f"iris-onprem-{server.server_key}",
                [f"iris/services/{service_id}" for service_id in service_ids],
            )
        except ExternalError as exc:
            # 세션 정책 크기 한도(서비스 25개쯤)에 걸렸는지 서비스 수로 가릴 수 있게 남긴다.
            # 예외 핸들러가 fields 를 구조화 로그로 한 번 남긴다.
            raise ExternalError(
                "ecr pull credential failed",
                onprem_server_id=server.id,
                service_count=len(service_ids),
            ) from exc
        logger.info(
            "onprem registry credentials issued",
            extra={
                "action": "issue_registry_credentials",
                "onprem_server_id": server.id,
                "service_count": len(service_ids),
            },
        )
        return RegistryCredentials(
            credential.registry,
            credential.username,
            credential.password,
            credential.expires_at,
            service_ids,
        )

    async def _record_seen(self, server: OnpremServer) -> None:
        """하트비트를 남기고 바로 커밋한다. 뒤에서 오류로 응답해도 남는다."""
        await self._onprem_server_repository.touch_last_seen(
            server.id, datetime.now(UTC), HEARTBEAT_WRITE_INTERVAL
        )
        await self._session.commit()

    async def _get_owned(
        self, owner_id: int, server_id: int, *, for_update: bool = False
    ) -> OnpremServer:
        server = await self._onprem_server_repository.find_by_id_and_owner_id(
            server_id, owner_id, for_update=for_update
        )
        if server is None:
            raise OnpremServerNotFoundError("onprem server not found", onprem_server_id=server_id)
        return server


def _validate_server_name(name: str, owner_id: int) -> None:
    """앞뒤 공백을 자른 이름이 규칙을 지키는지 본다. 사유 하나만 알리고 값은 오류에 담지 않는다."""
    if SERVER_NAME_PATTERN.fullmatch(name):
        return
    if not name:
        reason = "must not be blank"
    elif len(name) > SERVER_NAME_MAX_LENGTH:
        reason = f"must be at most {SERVER_NAME_MAX_LENGTH} characters"
    elif _SERVER_NAME_DIGITS_ONLY.fullmatch(name):
        reason = "must not be only digits"
    elif not _SERVER_NAME_FIRST.fullmatch(name[0]):
        reason = "must start with a letter, digit or Hangul syllable"
    else:
        reason = "may contain only letters, digits, Hangul syllables, '.', '_' and '-' (no spaces)"
    raise InvalidInputError(
        "invalid onprem server name", issues=[FieldIssue("name", reason)], owner_id=owner_id
    )


def _check_registration_token(
    server: OnpremServer | None,
    registration_token: str,
    allowed: tuple[OnpremServerStatus, ...],
) -> OnpremServer:
    """없음·만료·이미 연결됨을 구분하지 않고 같은 401 로 답한다."""
    if (
        server is None
        or not verify_url_token(registration_token, server.registration_token_hash)
        or server.is_registration_expired(datetime.now(UTC))
        or server.status not in allowed
    ):
        raise InvalidRegistrationTokenError("invalid registration token")
    return server


def _validate_connection(
    server: OnpremServer, tailnet_fqdn: str, api_ca_cert: str, sealed_secrets_cert: str
) -> None:
    """GitOps values·봉인에 그대로 들어가는 값이라 모양을 먼저 본다. 값은 오류에 담지 않는다."""
    issues: list[FieldIssue] = []
    hostname = tailscale_hostname(server.server_key)
    if not (
        tailnet_fqdn.startswith(hostname)
        and _TAILNET_SUFFIX.fullmatch(tailnet_fqdn.removeprefix(hostname))
    ):
        issues.append(FieldIssue("tailnetFqdn", f"must match {hostname}.<tailnet>.ts.net"))
    try:
        x509.load_pem_x509_certificates(api_ca_cert.strip().encode())
    except ValueError:
        issues.append(FieldIssue("apiCaCert", "must be a PEM certificate"))
    try:
        SecretSealer(sealed_secrets_cert, setting="sealedSecretsCert")
    except NotConfiguredError:
        issues.append(FieldIssue("sealedSecretsCert", "must be a PEM certificate with an RSA key"))
    if issues:
        raise InvalidInputError(
            "invalid onprem server connection", issues=issues, onprem_server_id=server.id
        )
