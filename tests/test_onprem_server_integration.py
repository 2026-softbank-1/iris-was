"""온프레미스 서버 등록 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

GitOps 저장소·Argo CD 는 test_deploy_flow 의 메모리 대역을 쓴다.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoAppStatus
from app.clients.secret_sealer import SecretSealer
from app.core.crypto import VariableCipher
from app.enums import JobKind, OnpremServerFailureCode, OnpremServerStatus
from app.models import OnpremServer, Project, Service, ServiceTarget, Target, User
from app.repositories.onprem_server_repository import OnpremServerRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.services.gitops_writer import GitOpsWriter
from app.services.onprem_server_service import (
    OnpremBootstrapSettings,
    OnpremServerRegistration,
    OnpremServerService,
)
from app.services.onprem_server_sync_service import MAX_GITOPS_ATTEMPTS, OnpremServerSyncService
from tests.fakes_onprem import CA_PEM, SEALED_SECRETS_CERT, SERVER_KEY
from tests.sealed_support import make_controller_key, unseal
from tests.test_deploy_flow import FakeArgo, FakeGitOps, Harness
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]

CIPHER = VariableCipher(Fernet.generate_key().decode())
PLATFORM_KEY, PLATFORM_CERT = make_controller_key()
BOOTSTRAP = OnpremBootstrapSettings("tskey", "v1.31.4+k3s1", "v1.7.2", "0.27.1")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


class World:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.gitops = FakeGitOps()
        self.argo = FakeArgo()
        self.sync = self.make_sync("worker-1")

    def make_sync(self, worker_id: str) -> OnpremServerSyncService:
        return OnpremServerSyncService(
            self.session_factory,
            self.gitops,  # type: ignore[arg-type]
            GitOpsWriter(self.gitops, 2, "org/gitops"),  # type: ignore[arg-type]
            self.argo,  # type: ignore[arg-type]
            worker_id,
            platform_sealer=SecretSealer(PLATFORM_CERT),
            cipher=CIPHER,
        )

    async def call[T](self, action: Any) -> T:
        """요청 하나처럼 새 세션으로 Control API 서비스를 연다."""
        async with self.session_factory() as session:
            service = OnpremServerService(
                session,
                OnpremServerRepository(session),
                TargetRepository(session),
                ServiceRepository(session),
                BOOTSTRAP,
                cipher=CIPHER,
            )
            result: T = await action(service)
            return result

    async def owner(self, github_id: int = 1) -> int:
        async with self.session_factory.begin() as session:
            user = await session.scalar(select(User).where(User.github_id == github_id))
            if user is None:
                user = await add(session, User(github_id=github_id, login=f"u{github_id}"))
            return user.id

    async def register(self, owner_id: int, name: str = "home-lab") -> OnpremServerRegistration:
        registration: OnpremServerRegistration = await self.call(
            lambda s: s.create_server(owner_id, name)
        )
        await self.connect(registration)
        return registration

    async def connect(self, registration: OnpremServerRegistration) -> None:
        key = registration.server.server_key
        await self.call(
            lambda s: s.connect(
                registration.registration_token,
                tailnet_fqdn=f"iris-{key}.tailb046e8.ts.net",
                api_ca_cert=CA_PEM,
                service_account_token="sa-token",
                sealed_secrets_cert=SEALED_SECRETS_CERT,
            )
        )

    async def run_next(self, sync: OnpremServerSyncService | None = None) -> OnpremServer | None:
        """다음 서버를 시각과 무관하게 바로 선점해 한 번 처리한다."""
        sync = sync or self.sync
        async with self.session_factory.begin() as session:
            await session.execute(
                update(OnpremServer)
                .where(OnpremServer.next_check_at.is_not(None))
                .values(next_check_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        server = await sync.claim_next_server()
        if server is None:
            return None
        try:
            await sync.run(server)
        except Exception as exc:
            await sync.retry_later(server, exc)
        return server

    async def load(self, server_id: int) -> OnpremServer:
        async with self.session_factory() as session:
            return await session.get_one(OnpremServer, server_id)

    def head_tree(self) -> dict[str, str]:
        return self.gitops.commits[self.gitops.head][1]


def _healthy() -> ArgoAppStatus:
    return ArgoAppStatus("Synced", None, "Healthy", None, None, None)


async def test_register_commits_sealed_values_then_connects_when_probe_is_healthy(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    key = registration.server.server_key

    await w.run_next()

    server = await w.load(registration.server.id)
    path = f"platform/onprem-servers/{key}"
    assert path in w.head_tree()
    assert server.gitops_commit_sha == w.gitops.head
    assert server.status == OnpremServerStatus.REGISTERING
    assert server.connect_deadline_at is not None
    assert server.locked_by is None
    values = json.loads(w.gitops.trees[w.head_tree()[path]]["values.yaml"])
    assert values["server"]["clusterName"] == f"onprem-{key}"
    config = json.loads(
        unseal(
            PLATFORM_KEY, values["cluster"]["encryptedConfig"], "argocd", f"cluster-onprem-{key}"
        )
    )
    assert config["bearerToken"] == "sa-token"
    assert config["tlsClientConfig"]["serverName"] == f"iris-{key}.tailb046e8.ts.net"

    w.argo.status = _healthy()
    await w.run_next()

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.CONNECTED
    assert server.connected_at is not None
    assert server.next_check_at is None
    assert await w.run_next() is None


async def test_crash_after_recording_commit_resumes_without_new_commit(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    w.gitops.fail_next_update = True

    await w.run_next()
    server = await w.load(registration.server.id)
    assert server.gitops_commit_sha is not None
    assert server.gitops_attempts == 1
    assert w.gitops.head == "c0"

    await w.run_next()

    server = await w.load(registration.server.id)
    assert w.gitops.head == server.gitops_commit_sha
    assert len(w.gitops.commits) == 2  # c0 + 값 커밋 하나
    assert server.connect_deadline_at is not None


async def test_probe_not_healthy_until_deadline_fails_with_connect_timed_out(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    await w.run_next()
    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(connect_deadline_at=datetime.now(UTC) - timedelta(1))
        )
    w.argo.status = ArgoAppStatus("Synced", None, "Progressing", None, None, None)

    await w.run_next()

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.FAILED
    assert server.failure_code == OnpremServerFailureCode.CONNECT_TIMED_OUT
    assert server.next_check_at is None


async def test_commit_failures_exhausted_fail_with_gitops_commit_failed(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    w.gitops.fail_create_tree = True

    for _ in range(MAX_GITOPS_ATTEMPTS):
        await w.run_next()

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.FAILED
    assert server.failure_code == OnpremServerFailureCode.GITOPS_COMMIT_FAILED
    assert server.last_error is not None
    assert w.gitops.head == "c0"


async def test_connect_again_while_syncing_discards_the_stale_result(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    async with session_factory.begin() as session:
        await session.execute(update(OnpremServer).values(next_check_at=datetime.now(UTC)))
    claimed = await w.sync.claim_next_server()
    assert claimed is not None

    await w.connect(registration)
    await w.sync.run(claimed)

    server = await w.load(registration.server.id)
    assert w.gitops.head == "c0"
    assert server.gitops_commit_sha is None
    assert server.locked_by is None
    assert server.connect_generation == 2

    await w.run_next()
    assert f"platform/onprem-servers/{server.server_key}" in w.head_tree()


async def test_claim_skips_leased_server_and_reclaims_after_lease_expires(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    await w.register(await w.owner())
    other = w.make_sync("worker-2")

    first = await w.sync.claim_next_server()
    assert first is not None
    assert await other.claim_next_server() is None

    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    reclaimed = await other.claim_next_server()
    assert reclaimed is not None and reclaimed.locked_by == "worker-2"


async def test_delete_removes_server_directory_and_hides_target(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    registration = await w.register(owner_id)
    await w.run_next()
    key = registration.server.server_key
    assert f"platform/onprem-servers/{key}" in w.head_tree()

    await w.call(lambda s: s.delete_server(owner_id, registration.server.id))
    await w.run_next()

    server = await w.load(registration.server.id)
    assert f"platform/onprem-servers/{key}" not in w.head_tree()
    assert server.gitops_commit_sha == w.gitops.head
    assert server.next_check_at is None
    async with session_factory() as session:
        visible = await TargetRepository(session).search_visible(owner_id)
    assert registration.server.target_id not in [t.id for t in visible]


async def test_delete_never_committed_server_skips_commit(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    registration: OnpremServerRegistration = await w.call(
        lambda s: s.create_server(owner_id, "home-lab")
    )

    await w.call(lambda s: s.delete_server(owner_id, registration.server.id))
    await w.run_next()

    server = await w.load(registration.server.id)
    assert w.gitops.head == "c0"
    assert server.next_check_at is None


async def test_targets_visible_per_owner_and_server_in_use(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id, other_id = await w.owner(1), await w.owner(2)
    registration = await w.register(owner_id)
    target_id = registration.server.target_id

    async with session_factory() as session:
        targets = TargetRepository(session)
        mine = [t.name for t in await targets.search_visible(owner_id)]
        theirs = [t.name for t in await targets.search_visible(other_id)]
        assert await targets.search_visible_by_ids([target_id], other_id) == []
    assert f"onprem-{registration.server.server_key}" in mine
    assert "aws" in theirs and len(theirs) == len(mine) - 1

    async with session_factory.begin() as session:
        service = await seed_service(session, owner_github_id=1)
        session.add(ServiceTarget(service_id=service.id, target_id=target_id))
    async with session_factory() as session:
        services = ServiceRepository(session)
        assert await services.is_target_in_use(target_id)
        assert await services.search_ids_by_target_id(target_id) == [service.id]
        found = await services.find_deploy_target_server(service.id)
        assert found is not None and found.id == registration.server.id


async def test_deploy_to_server_target_uses_server_host_and_server_sealing_key(
    session_factory: Any,
) -> None:
    global_key, global_cert = make_controller_key()
    h = Harness(session_factory, cipher=CIPHER, sealer=SecretSealer(global_cert))
    await h.request_deploy(variables_snapshot={"DATABASE_URL": CIPHER.encrypt("postgres://x")})
    async with session_factory() as session:
        owner_id = await session.scalar(
            select(Project.owner_id).join(Service).where(Service.id == h.service_id)
        )
    w = World(session_factory)
    registration = await w.register(owner_id)
    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(status=OnpremServerStatus.CONNECTED, next_check_at=None)
        )
        session.add(ServiceTarget(service_id=h.service_id, target_id=registration.server.target_id))

    await h.run_next(JobKind.DEPLOY)

    key = registration.server.server_key
    tree = h.gitops.commits[h.gitops.head][1]
    values = json.loads(
        h.gitops.trees[tree[f"services/{h.service_id}/onprem-{key}"]]["values.yaml"]
    )
    assert values["route"]["host"] == f"web-{h.service_id}-{key}.internal.likelion.uk"
    assert values["iris"]["targetName"] == f"onprem-{key}"
    variables = values["variables"]
    sealed = variables["encryptedData"]["DATABASE_URL"]
    namespace = f"svc-{h.service_id}"
    assert unseal(SERVER_KEY, sealed, namespace, variables["name"]) == "postgres://x"
    with pytest.raises(ValueError):
        unseal(global_key, sealed, namespace, variables["name"])
    async with session_factory() as session:
        target = await session.get_one(Target, registration.server.target_id)
    assert target.owner_id == owner_id
