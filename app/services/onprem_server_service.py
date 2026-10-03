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
from app.core.crypto import VariableCipher
from app.core.exceptions import (
    ExternalError,
    FieldIssue,
    InvalidInputError,
    InvalidRegistrationTokenError,
    InvalidStatusTransitionError,
    NotConfiguredError,
    OnpremServerInUseError,
    OnpremServerNameConflictError,
    OnpremServerNotConnectedError,
    OnpremServerNotFoundError,
    UnauthorizedError,
)
from app.core.security import generate_url_token, hash_url_token, verify_url_token
from app.enums import OnpremServerStatus, TargetKind
from app.models.onprem_server import OnpremServer
from app.models.target import Target
from app.repositories.onprem_server_repository import OnpremServerRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository

logger = logging.getLogger(__name__)

REGISTRATION_TOKEN_TTL = timedelta(hours=24)
ONPREM_DOMAIN_SUFFIX = "internal.likelion.uk"
TAILSCALE_TAGS = ("tag:iris-onprem",)
INSTALL_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "assets" / "onprem" / "install.sh"
# 첫 글자 영문 + 영문·숫자 7자. host 의 마지막 `-` 뒤가 이 모양이면 게이트웨이가 서버로 보낸다.
_SERVER_KEY_FIRST = string.ascii_lowercase
_SERVER_KEY_REST = string.ascii_lowercase + string.digits
_SERVER_KEY_LENGTH = 8
_REISSUABLE_STATUSES = (OnpremServerStatus.PENDING, OnpremServerStatus.FAILED)
# connect 뒤 스크립트가 중간에 실패해도 같은 명령으로 다시 돌릴 수 있게 REGISTERING 도 받는다.
# bootstrap 은 상태를 바꾸지 않고, connect 는 같은 토큰으로 다시 보내도 덮어쓴다.
_RERUNNABLE_STATUSES = (
    OnpremServerStatus.PENDING,
    OnpremServerStatus.REGISTERING,
    OnpremServerStatus.FAILED,
)
_FQDN_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


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

    # --- 사용자 API

    async def create_server(self, owner_id: int, name: str) -> OnpremServerRegistration:
        """서버와 전용 타깃을 한 트랜잭션에서 만든다. 등록 토큰은 24시간 유효하다."""
        if await self._onprem_server_repository.find_by_owner_id_and_name(owner_id, name):
            raise OnpremServerNameConflictError("onprem server name already exists", name=name)
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
        """PENDING·FAILED 일 때만. 이전 토큰은 무효가 되고 상태는 PENDING 이다."""
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

        비밀이 틀리면 401, 맞지만 아직 CONNECTED 가 아니면 409 다(서버의 CronJob 이 다음 회차를
        기다린다).
        """
        if self._ecr_pull_client is None:
            raise NotConfiguredError(
                "ecr pull role is not configured",
                setting="AWS_REGION, ONPREM_ECR_PULL_ROLE_ARN",
            )
        server = await self._onprem_server_repository.find_by_server_secret_hash(
            hash_url_token(server_secret)
        )
        if (
            server is None
            or server.server_secret_hash is None
            or not verify_url_token(server_secret, server.server_secret_hash)
        ):
            raise UnauthorizedError("invalid server secret")
        if server.status != OnpremServerStatus.CONNECTED:
            raise OnpremServerNotConnectedError(
                "onprem server is not connected",
                onprem_server_id=server.id,
                onprem_server_status=server.status,
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

    async def _get_owned(
        self, owner_id: int, server_id: int, *, for_update: bool = False
    ) -> OnpremServer:
        server = await self._onprem_server_repository.find_by_id_and_owner_id(
            server_id, owner_id, for_update=for_update
        )
        if server is None:
            raise OnpremServerNotFoundError("onprem server not found", onprem_server_id=server_id)
        return server


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
    labels = tailnet_fqdn.split(".")
    if (
        len(tailnet_fqdn) > 253
        or len(labels) < 2
        or labels[0] != tailscale_hostname(server.server_key)
        or not all(_FQDN_LABEL.fullmatch(label) for label in labels)
    ):
        issues.append(FieldIssue("tailnetFqdn", f"must start with iris-{server.server_key}."))
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
