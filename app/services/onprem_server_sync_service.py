"""사용자가 등록한 온프레미스 서버를 GitOps 에 반영하고 연결을 확인한다(ADR 0029).

Deploy Worker 만 쓴다.

jobs 큐는 배포 요청에 묶여 있어(`jobs.deployment_request_id` NOT NULL) 서버 작업은 onprem_servers
행 자체를 lease 로 선점한다. 트랜잭션은 선점·기록마다 짧게 끝내고 GitHub·Argo CD 호출은 밖에서 한다.

- REGISTERING: `platform/onprem-servers/{key}/values.yaml` 커밋 → probe Application 이
  Synced+Healthy 면 CONNECTED, 기한(커밋 반영 후 15분)이 지나면 FAILED(CONNECT_TIMED_OUT).
  커밋 재시도를 소진하면 FAILED(GITOPS_COMMIT_FAILED).
- 삭제됨: 서버 디렉터리를 지우는 커밋.

외부에 쓰기 전에 커밋 SHA 를 먼저 기록해 Worker 가 죽어도 중복 커밋 없이 이어서 처리한다.
connect 를 다시 받았거나(connect_generation) 삭제됐으면 선점할 때의 결과는 버리고 다음에 다시 한다.
"""

import base64
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoCdClient
from app.clients.github_client import GitHubClient
from app.clients.secret_sealer import SecretSealer
from app.core.crypto import VariableCipher
from app.core.exceptions import ExternalError, NotConfiguredError
from app.enums import OnpremServerFailureCode, OnpremServerStatus
from app.models.onprem_server import OnpremServer
from app.repositories.onprem_server_repository import OnpremServerRepository
from app.services.gitops_writer import GitOpsWriter
from app.services.onprem_server_service import onprem_target_name

logger = logging.getLogger(__name__)

# 기록할 때마다(커밋 SHA·기한) 갱신한다. 커밋 한 번(GitHub 호출 몇 번)보다 넉넉히 둔다.
SERVER_LEASE = timedelta(minutes=5)
CONNECT_TIMEOUT = timedelta(minutes=15)
PROBE_INTERVAL = timedelta(seconds=10)
# probe 를 읽을 토큰이 없을 때 다시 볼 간격. 그동안 기한은 미룬다.
PROBE_DISABLED_INTERVAL = timedelta(minutes=1)
RETRY_BASE_DELAY = timedelta(seconds=30)
MAX_GITOPS_ATTEMPTS = 5
VALUES_FILE_NAME = "values.yaml"
# management Argo CD 의 cluster Secret 위치. chart `iris-onprem-server` 가 같은 이름으로 푼다.
ARGOCD_NAMESPACE = "argocd"
K3S_API_PORT = 6443
APPS_PORT = 80
_MAX_ERROR_LENGTH = 1000


def server_directory(server_key: str) -> str:
    return f"platform/onprem-servers/{server_key}"


def probe_application_name(server_key: str) -> str:
    return f"iris-onprem-probe-{server_key}"


def cluster_secret_name(server_key: str) -> str:
    return f"cluster-onprem-{server_key}"


def build_cluster_config(bearer_token: str, ca_pem: str, tailnet_fqdn: str) -> str:
    """Argo CD cluster Secret 의 `config`. 봉인하기 전 평문이라 메모리에만 둔다."""
    return json.dumps(
        {
            "bearerToken": bearer_token,
            "tlsClientConfig": {"caData": _base64(ca_pem), "serverName": tailnet_fqdn},
        },
        sort_keys=True,
    )


def render_server_values(
    *, server_key: str, tailnet_fqdn: str, ca_pem: str, encrypted_config: str
) -> str:
    """`platform/onprem-servers/{key}/values.yaml`. chart `iris-onprem-server` 의 values 다.

    JSON 은 YAML 이다. 클러스터 토큰은 management controller 로 봉인한 값만 담는다.
    """
    values = {
        "server": {
            "key": server_key,
            "clusterName": onprem_target_name(server_key),
            "tailnetFqdn": tailnet_fqdn,
            "apiPort": K3S_API_PORT,
            "appsPort": APPS_PORT,
        },
        "cluster": {"caData": _base64(ca_pem), "encryptedConfig": encrypted_config},
    }
    return json.dumps(values, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class _Claim:
    """선점할 때의 서버 상태. 기록할 때 이것과 같은지 다시 본다."""

    server_id: int
    server_key: str
    connect_generation: int
    is_deleted: bool

    @classmethod
    def of(cls, server: OnpremServer) -> "_Claim":
        return cls(server.id, server.server_key, server.connect_generation, server.is_deleted)

    def matches(self, server: OnpremServer) -> bool:
        return (
            server.connect_generation == self.connect_generation
            and server.is_deleted == self.is_deleted
        )


class _StaleClaimError(Exception):
    """선점한 뒤 connect 를 다시 받았거나 삭제됐다. 이번 결과는 버린다."""


class _AlreadyRemovedError(Exception):
    """GitOps HEAD 에 서버 디렉터리가 이미 없다."""


class OnpremServerSyncService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        gitops: GitOpsWriter,
        probe_argocd: ArgoCdClient | None,
        worker_id: str,
        *,
        platform_sealer: SecretSealer,
        cipher: VariableCipher | None,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._gitops = gitops
        # probe Application 의 Argo project 를 읽는 전용 토큰의 Client. 없으면 연결 확인을 안 한다.
        self._probe_argocd = probe_argocd
        self._worker_id = worker_id
        self._platform_sealer = platform_sealer
        # 서버의 ServiceAccount 토큰(암호문)을 푼다. Control API 와 같은 키다.
        self._cipher = cipher

    async def claim_next_server(self) -> OnpremServer | None:
        async with self._session_factory.begin() as session:
            return await OnpremServerRepository(session).claim_next_due(
                self._worker_id, SERVER_LEASE
            )

    async def find_seconds_until_next_check(self) -> float | None:
        async with self._session_factory() as session:
            return await OnpremServerRepository(session).find_seconds_until_next_check()

    async def run(self, server: OnpremServer) -> None:
        """선점한 서버를 한 번 처리하고 lease 를 놓는다."""
        claim = _Claim.of(server)
        try:
            if server.is_deleted:
                await self._remove(claim, server)
            elif server.status == OnpremServerStatus.REGISTERING:
                await self._register(claim, server)
            else:
                await self._update(claim, lambda s: s.schedule_check(None))
        except _StaleClaimError:
            logger.info("onprem server changed while syncing", extra={"action": "sync_server"})
            await self._release(claim)

    async def retry_later(self, server: OnpremServer, error: Exception) -> None:
        """GitOps 커밋 밖의 실패(설정·복호화 등). 오류를 남기고 다음 확인을 미루며 lease 를 놓는다.

        커밋 재시도 횟수는 쓰지 않는다. 설정을 고치면 그대로 이어서 처리한다.
        """
        claim = _Claim.of(server)
        description = _describe(error)
        try:
            await self._update(
                claim,
                lambda s: s.record_error(description, datetime.now(UTC) + RETRY_BASE_DELAY),
            )
        except _StaleClaimError:
            await self._release(claim)

    # --- 등록

    async def _register(self, claim: _Claim, server: OnpremServer) -> None:
        if server.connect_deadline_at is None:
            # 봉인·복호화 실패는 커밋 실패가 아니다. Worker 루프의 retry_later 로 넘긴다.
            values = await self._render_values(server)
            try:
                await self._push_values(claim, server, values)
            except _StaleClaimError:
                raise
            except Exception as exc:
                await self._record_gitops_failure(claim, server, exc)
                return
            deadline = datetime.now(UTC) + CONNECT_TIMEOUT
            # probe 를 확인할 때까지 lease 를 쥐고 있는다.
            await self._update(claim, lambda s: s.confirm_gitops_commit(deadline), release=False)
            server.connect_deadline_at = deadline
            logger.info(
                "onprem server values committed",
                extra={"action": "register_server", "onprem_server_id": claim.server_id},
            )
        assert server.connect_deadline_at is not None
        await self._check_probe(claim, server.connect_deadline_at)

    async def _push_values(self, claim: _Claim, server: OnpremServer, values: str) -> None:
        token = await self._gitops.token()
        repository = self._gitops.repository
        path = server_directory(claim.server_key)

        async def create(head_sha: str) -> str:
            tree_sha = await self._github.create_tree(token, repository, {VALUES_FILE_NAME: values})
            return await self._github.create_commit(
                token,
                repository,
                head_sha,
                path,
                tree_sha,
                _commit_message(f"register onprem server {claim.server_key}", claim),
            )

        async def record(commit_sha: str) -> None:
            await self._update(claim, lambda s: s.record_gitops_commit(commit_sha), release=False)

        await self._gitops.push(token, server.gitops_commit_sha, create, record)

    async def _render_values(self, server: OnpremServer) -> str:
        if self._cipher is None:
            raise NotConfiguredError(
                "service account token cannot be decrypted", setting="VARIABLES_ENCRYPTION_KEY"
            )
        assert server.tailnet_fqdn is not None and server.api_ca_cert is not None
        assert server.encrypted_service_account_token is not None
        config = build_cluster_config(
            self._cipher.decrypt(server.encrypted_service_account_token),
            server.api_ca_cert,
            server.tailnet_fqdn,
        )
        sealed = await self._platform_sealer.seal(
            ARGOCD_NAMESPACE, cluster_secret_name(server.server_key), {"config": config}
        )
        return render_server_values(
            server_key=server.server_key,
            tailnet_fqdn=server.tailnet_fqdn,
            ca_pem=server.api_ca_cert,
            encrypted_config=sealed["config"],
        )

    async def _record_gitops_failure(
        self, claim: _Claim, server: OnpremServer, error: Exception
    ) -> None:
        """재시도를 소진하면 FAILED(GITOPS_COMMIT_FAILED), 아니면 지수 백오프로 다시 한다."""
        description = _describe(error)
        attempts = server.gitops_attempts + 1
        if attempts >= MAX_GITOPS_ATTEMPTS:
            await self._update(
                claim, lambda s: s.fail(OnpremServerFailureCode.GITOPS_COMMIT_FAILED, description)
            )
            logger.error(
                "onprem server commit gave up",
                extra={
                    "action": "register_server",
                    "onprem_server_id": claim.server_id,
                    "error": description,
                },
            )
            return
        retry_at = datetime.now(UTC) + RETRY_BASE_DELAY * 2 ** (attempts - 1)
        await self._update(claim, lambda s: s.record_gitops_failure(description, retry_at))
        logger.warning(
            "onprem server commit failed",
            extra={
                "action": "register_server",
                "onprem_server_id": claim.server_id,
                "attempts": attempts,
                "error": description,
            },
        )

    async def _check_probe(self, claim: _Claim, deadline_at: datetime) -> None:
        """probe Application 이 Synced+Healthy 면 CONNECTED, 기한이 지나면 FAILED 다.

        probe 를 읽을 토큰이 없으면 확인하지 않고 REGISTERING 으로 둔다. 기한은 그만큼 미뤄, 토큰이
        생긴 뒤에도 연결 확인 시간을 온전히 준다.
        """
        now = datetime.now(UTC)
        if self._probe_argocd is None:
            logger.warning(
                "onprem server connection check is off",
                extra={
                    "action": "register_server",
                    "onprem_server_id": claim.server_id,
                    "setting": "ARGOCD_PROBE_TOKEN",
                },
            )
            await self._update(
                claim,
                lambda s: s.postpone_connect_check(
                    now + PROBE_DISABLED_INTERVAL, now + CONNECT_TIMEOUT
                ),
            )
            return
        try:
            status = await self._probe_argocd.get_application(
                probe_application_name(claim.server_key)
            )
        except ExternalError:
            logger.warning(
                "probe status check failed",
                exc_info=True,
                extra={"action": "register_server", "onprem_server_id": claim.server_id},
            )
            status = None
        if (
            status is not None
            and status.sync_status == "Synced"
            and (status.health_status == "Healthy")
        ):
            await self._update(claim, lambda s: s.mark_as_connected(now))
            logger.info(
                "onprem server connected",
                extra={"action": "register_server", "onprem_server_id": claim.server_id},
            )
        elif now > deadline_at:
            await self._update(claim, lambda s: s.fail(OnpremServerFailureCode.CONNECT_TIMED_OUT))
            logger.info(
                "onprem server connect timed out",
                extra={
                    "action": "register_server",
                    "onprem_server_id": claim.server_id,
                    "argo_sync_status": status and status.sync_status,
                    "argo_health_status": status and status.health_status,
                },
            )
        else:
            await self._update(claim, lambda s: s.schedule_check(now + PROBE_INTERVAL))

    # --- 삭제

    async def _remove(self, claim: _Claim, server: OnpremServer) -> None:
        """서버 디렉터리를 지우는 커밋. 이미 없으면 건너뛴다. 재시도를 소진하면 운영자 몫이다."""
        try:
            await self._delete_directory(claim, server)
        except _StaleClaimError:
            raise
        except Exception as exc:
            description = _describe(exc)
            attempts = server.gitops_attempts + 1
            if attempts >= MAX_GITOPS_ATTEMPTS:
                await self._update(claim, lambda s: s.stop_checks(description))
                logger.error(
                    "onprem server cleanup gave up",
                    extra={
                        "action": "remove_server",
                        "onprem_server_id": claim.server_id,
                        "error": description,
                    },
                )
                return
            retry_at = datetime.now(UTC) + RETRY_BASE_DELAY * 2 ** (attempts - 1)
            await self._update(claim, lambda s: s.record_gitops_failure(description, retry_at))
            return
        await self._update(claim, lambda s: s.schedule_check(None))
        logger.info(
            "onprem server directory removed",
            extra={"action": "remove_server", "onprem_server_id": claim.server_id},
        )

    async def _delete_directory(self, claim: _Claim, server: OnpremServer) -> None:
        token = await self._gitops.token()
        repository = self._gitops.repository
        path = server_directory(claim.server_key)

        async def create(head_sha: str) -> str:
            if await self._github.find_subtree_sha(token, repository, head_sha, path) is None:
                raise _AlreadyRemovedError
            return await self._github.create_delete_commit(
                token,
                repository,
                head_sha,
                path,
                _commit_message(f"remove onprem server {claim.server_key}", claim),
            )

        async def record(commit_sha: str) -> None:
            await self._update(claim, lambda s: s.record_gitops_commit(commit_sha), release=False)

        with contextlib.suppress(_AlreadyRemovedError):
            await self._gitops.push(token, server.gitops_commit_sha, create, record)

    # --- 공용

    async def _update(
        self,
        claim: _Claim,
        change: Callable[[OnpremServer], None],
        *,
        release: bool = True,
    ) -> None:
        """행을 잠가 선점할 때와 같은지 확인하고 바꾼다. release 면 lease 를 놓고, 아니면 갱신한다.

        lease 를 다른 Worker 가 가져갔거나(만료) 토큰 재발급으로 비워졌으면 결과를 쓰지 않는다.
        """
        async with self._session_factory.begin() as session:
            server = await OnpremServerRepository(session).get_by_id_for_update(claim.server_id)
            if not claim.matches(server) or server.locked_by != self._worker_id:
                raise _StaleClaimError
            change(server)
            if release:
                server.release_lease()
            else:
                server.renew_lease(datetime.now(UTC) + SERVER_LEASE)

    async def _release(self, claim: _Claim) -> None:
        """결과를 쓰지 않고 lease 만 놓는다. 다음 확인 시각은 바꾼 쪽이 정했다."""
        async with self._session_factory.begin() as session:
            server = await OnpremServerRepository(session).get_by_id_for_update(claim.server_id)
            _release_own_lease(server, self._worker_id)


def _release_own_lease(server: OnpremServer, worker_id: str) -> None:
    if server.locked_by == worker_id:
        server.release_lease()


def _base64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _commit_message(subject: str, claim: _Claim) -> str:
    return f"{subject}\n\nIris-Onprem-Server-Id: {claim.server_id}\n"


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:_MAX_ERROR_LENGTH]
