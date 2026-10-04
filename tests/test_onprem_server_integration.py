"""온프레미스 서버 등록 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

GitOps 저장소·Argo CD 는 test_deploy_flow 의 메모리 대역을 쓴다.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoAppStatus
from app.clients.secret_sealer import SecretSealer
from app.clients.tailscale_client import TailscaleApiError, TailscaleDevice
from app.core.crypto import VariableCipher
from app.core.exceptions import InvalidInputError, OnpremServerNameConflictError
from app.enums import (
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    JobKind,
    OnpremServerFailureCode,
    OnpremServerStatus,
    TargetKind,
)
from app.models import (
    DeploymentRequest,
    OnpremMetricSample,
    OnpremServer,
    Project,
    Service,
    ServiceTarget,
    Target,
    User,
)
from app.repositories.onprem_metric_sample_repository import OnpremMetricSampleRepository
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
from tests.fakes_onprem import CA_PEM, LEGACY_SERVER_NAMES, SEALED_SECRETS_CERT, SERVER_KEY
from tests.sealed_support import make_controller_key, unseal
from tests.test_deploy_flow import SETTINGS, FakeArgo, FakeGitOps, Harness
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


class FakeTailscale:
    """Tailscale API 대역. 지운 기기 id 를 기록하고, fail 이 남아 있으면 그만큼 실패한다."""

    def __init__(self, devices: list[TailscaleDevice] | None = None) -> None:
        self.devices = list(devices or [])
        self.deleted: list[str] = []
        self.fail = 0

    async def search_devices(self) -> list[TailscaleDevice]:
        if self.fail:
            self.fail -= 1
            raise TailscaleApiError("tailscale request failed", status_code=500)
        return list(self.devices)

    async def delete_device(self, device_id: str) -> None:
        self.deleted.append(device_id)
        self.devices = [d for d in self.devices if d.id != device_id]


class World:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tailscale: FakeTailscale | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gitops = FakeGitOps()
        self.argo = FakeArgo()
        self.tailscale = tailscale
        self.sync = self.make_sync("worker-1")

    def make_sync(
        self, worker_id: str, *, has_probe: bool = True, cipher: VariableCipher = CIPHER
    ) -> OnpremServerSyncService:
        return OnpremServerSyncService(
            self.session_factory,
            self.gitops,  # type: ignore[arg-type]
            GitOpsWriter(self.gitops, 2, "org/gitops"),  # type: ignore[arg-type]
            self.argo if has_probe else None,  # type: ignore[arg-type]
            worker_id,
            platform_sealer=SecretSealer(PLATFORM_CERT),
            cipher=cipher,
            tailscale=self.tailscale,  # type: ignore[arg-type]
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


async def test_without_probe_token_server_stays_registering_past_deadline(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    w.sync = w.make_sync("worker-1", has_probe=False)
    registration = await w.register(await w.owner())
    await w.run_next()
    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(connect_deadline_at=datetime.now(UTC) - timedelta(1))
        )
    w.argo.status = _healthy()

    await w.run_next()

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.REGISTERING
    assert server.connect_deadline_at is not None
    assert server.connect_deadline_at > datetime.now(UTC) + timedelta(minutes=14)
    assert server.next_check_at is not None
    assert server.locked_by is None


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
    # 플래그를 켠 Worker 라도 서버 타깃(ONPREM)에는 deploymentStrategy·프로젝트 통신 키를
    # 쓰지 않는다.
    h = Harness(
        session_factory,
        cipher=CIPHER,
        sealer=SecretSealer(global_cert),
        settings=SETTINGS.model_copy(
            update={"deployment_strategy_enabled": True, "project_networking_enabled": True}
        ),
    )
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
    assert values["imagePullSecrets"] == [{"name": "iris-ecr-pull"}]
    assert "deploymentStrategy" not in values
    # 프로젝트 통신(chart 0.9.0) 키도 서버 타깃에는 쓰지 않는다.
    assert not {"projectId", "service", "hostAliases", "workload", "database"} & set(values)
    variables = values["variables"]
    sealed = variables["encryptedData"]["DATABASE_URL"]
    namespace = f"svc-{h.service_id}"
    assert unseal(SERVER_KEY, sealed, namespace, variables["name"]) == "postgres://x"
    with pytest.raises(ValueError):
        unseal(global_key, sealed, namespace, variables["name"])
    async with session_factory() as session:
        target = await session.get_one(Target, registration.server.target_id)
    assert target.owner_id == owner_id

    # 서비스를 지워도 GitOps 에 커밋한 release 가 있으면 서버는 아직 쓰는 중이다.
    target_id = registration.server.target_id
    async with session_factory.begin() as session:
        await session.execute(update(DeploymentRequest).values(status=DeploymentStatus.SUCCEEDED))
        await session.execute(
            update(Service).where(Service.id == h.service_id).values(is_deleted=True)
        )
    async with session_factory() as session:
        assert await ServiceRepository(session).is_target_in_use(target_id)

    async with session_factory.begin() as session:
        session.add(
            DeploymentRequest(
                service_id=h.service_id,
                environment=Environment.PROD,
                source_sha="a" * 40,
                trigger_type=DeploymentTrigger.REMOVE,
                idempotency_key="remove-1",
                status=DeploymentStatus.SUCCEEDED,
            )
        )
    async with session_factory() as session:
        assert not await ServiceRepository(session).is_target_in_use(target_id)


async def test_render_failure_records_error_without_using_commit_attempts(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    w.sync = w.make_sync("worker-1", cipher=VariableCipher(Fernet.generate_key().decode()))
    registration = await w.register(await w.owner())

    await w.run_next()

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.REGISTERING
    assert server.gitops_attempts == 0
    assert server.last_error is not None and "Decryption" in server.last_error
    assert server.next_check_at is not None and server.next_check_at > datetime.now(UTC)
    assert server.locked_by is None
    assert w.gitops.head == "c0"


async def test_lease_is_renewed_when_commit_is_recorded(session_factory: Any) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    seen: list[datetime | None] = []
    original = w.gitops.update_branch

    async def update_branch(token: str, full_name: str, branch: str, commit_sha: str) -> None:
        seen.append((await w.load(registration.server.id)).locked_until)
        await original(token, full_name, branch, commit_sha)

    w.gitops.update_branch = update_branch  # type: ignore[method-assign]
    async with session_factory.begin() as session:
        await session.execute(update(OnpremServer).values(next_check_at=datetime.now(UTC)))
    claimed = await w.sync.claim_next_server()
    assert claimed is not None
    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(locked_until=datetime.now(UTC) + timedelta(seconds=5))
        )

    await w.sync.run(claimed)

    assert seen and seen[0] is not None
    assert seen[0] > datetime.now(UTC) + timedelta(minutes=4)


async def test_reissue_while_syncing_drops_the_worker_result(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    registration = await w.register(owner_id)
    async with session_factory.begin() as session:
        await session.execute(update(OnpremServer).values(next_check_at=datetime.now(UTC)))
    claimed = await w.sync.claim_next_server()
    assert claimed is not None

    await w.call(lambda s: s.reissue_registration_token(owner_id, registration.server.id))
    await w.sync.run(claimed)

    server = await w.load(registration.server.id)
    assert server.status == OnpremServerStatus.PENDING
    assert server.gitops_commit_sha is None
    assert w.gitops.head == "c0"
    assert await w.sync.claim_next_server() is None


async def test_worker_whose_lease_was_taken_over_writes_nothing(session_factory: Any) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    async with session_factory.begin() as session:
        await session.execute(update(OnpremServer).values(next_check_at=datetime.now(UTC)))
    slow = await w.sync.claim_next_server()
    assert slow is not None
    async with session_factory.begin() as session:
        await session.execute(
            update(OnpremServer).values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    other = w.make_sync("worker-2")
    assert await other.claim_next_server() is not None

    await w.sync.run(slow)

    server = await w.load(registration.server.id)
    assert w.gitops.head == "c0"
    assert server.gitops_commit_sha is None
    assert server.locked_by == "worker-2"


async def _count(session_factory: Any, model: Any, owner_id: int) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.owner_id == owner_id)
        )
    return int(count or 0)


async def test_create_server_same_name_conflicts_and_leaves_no_extra_rows(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id, other_id = await w.owner(1), await w.owner(2)
    await w.call(lambda s: s.create_server(owner_id, "e2e-dup"))

    with pytest.raises(OnpremServerNameConflictError):
        await w.call(lambda s: s.create_server(owner_id, "e2e-dup"))
    await w.call(lambda s: s.create_server(other_id, "e2e-dup"))

    assert await _count(session_factory, OnpremServer, owner_id) == 1
    assert await _count(session_factory, Target, owner_id) == 1
    assert await _count(session_factory, OnpremServer, other_id) == 1


async def test_create_server_same_name_after_delete_is_allowed(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    first: OnpremServerRegistration = await w.call(lambda s: s.create_server(owner_id, "e2e-dup"))
    await w.call(lambda s: s.delete_server(owner_id, first.server.id))

    second: OnpremServerRegistration = await w.call(lambda s: s.create_server(owner_id, "e2e-dup"))

    assert second.server.id != first.server.id
    servers: list[OnpremServer] = await w.call(lambda s: s.search_servers(owner_id))
    assert [server.id for server in servers] == [second.server.id]
    with pytest.raises(OnpremServerNameConflictError):
        await w.call(lambda s: s.create_server(owner_id, "e2e-dup"))


async def test_create_server_concurrent_same_name_creates_one_and_conflicts_the_other(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()

    results = await asyncio.gather(
        w.call(lambda s: s.create_server(owner_id, "e2e-dup")),
        w.call(lambda s: s.create_server(owner_id, "e2e-dup")),
        return_exceptions=True,
    )

    conflicts = [r for r in results if isinstance(r, OnpremServerNameConflictError)]
    created = [r for r in results if isinstance(r, OnpremServerRegistration)]
    assert len(conflicts) == 1 and len(created) == 1
    assert await _count(session_factory, OnpremServer, owner_id) == 1
    assert await _count(session_factory, Target, owner_id) == 1


async def _add_server_row(
    session: AsyncSession,
    owner_id: int,
    *,
    name: str,
    server_key: str,
    target_key: str | None = None,
) -> OnpremServer:
    """사전 조회를 거치지 않고 Repository 로 바로 넣는다. 유일 인덱스가 거절하는지 본다."""
    target = await TargetRepository(session).add(
        Target(
            name=f"onprem-{target_key or server_key}",
            kind=TargetKind.ONPREM,
            domain_suffix="internal.likelion.uk",
            owner_id=owner_id,
        )
    )
    return await OnpremServerRepository(session).save(
        OnpremServer(
            owner_id=owner_id,
            name=name,
            server_key=server_key,
            target_id=target.id,
            status=OnpremServerStatus.PENDING,
            registration_token_hash=f"hash-{name}-{server_key}",
            registration_expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )


async def test_save_duplicate_name_violating_the_index_raises_name_conflict(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    async with session_factory.begin() as session:
        await _add_server_row(session, owner_id, name="e2e-dup", server_key="aaaaaaaa")

    async with session_factory() as session:
        with pytest.raises(OnpremServerNameConflictError) as raised:
            await _add_server_row(session, owner_id, name="e2e-dup", server_key="bbbbbbbb")
        await session.rollback()

    assert isinstance(raised.value.__cause__, IntegrityError)
    assert await _count(session_factory, OnpremServer, owner_id) == 1


async def test_save_duplicate_name_of_deleted_server_is_allowed_by_partial_index(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    async with session_factory.begin() as session:
        first = await _add_server_row(session, owner_id, name="e2e-dup", server_key="aaaaaaaa")
        first.mark_as_deleted()

    async with session_factory.begin() as session:
        await _add_server_row(session, owner_id, name="e2e-dup", server_key="bbbbbbbb")

    assert await _count(session_factory, OnpremServer, owner_id) == 2


async def test_save_duplicate_server_key_is_not_a_name_conflict(session_factory: Any) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    async with session_factory.begin() as session:
        await _add_server_row(session, owner_id, name="first", server_key="aaaaaaaa")

    async with session_factory() as session:
        # 이름이 달라 이름 인덱스는 어기지 않고 server_key 의 unique 제약만 어긴다.
        with pytest.raises(IntegrityError) as raised:
            await _add_server_row(
                session, owner_id, name="second", server_key="aaaaaaaa", target_key="bbbbbbbb"
            )
        await session.rollback()

    assert "uq_onprem_servers_server_key" in str(raised.value)
    assert await _count(session_factory, OnpremServer, owner_id) == 1


async def _names(session_factory: Any, owner_id: int) -> list[str]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(OnpremServer.name)
            .where(OnpremServer.owner_id == owner_id)
            .order_by(OnpremServer.id)
        )
        return list(rows)


async def test_create_server_stores_trimmed_name_and_conflicts_on_the_trimmed_value(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()

    first: OnpremServerRegistration = await w.call(
        lambda s: s.create_server(owner_id, " home-lab ")
    )

    assert first.server.name == "home-lab"
    assert await _names(session_factory, owner_id) == ["home-lab"]
    for duplicate in ("home-lab", "  home-lab", "home-lab\n"):
        with pytest.raises(OnpremServerNameConflictError):
            await w.call(lambda s, name=duplicate: s.create_server(owner_id, name))
    assert await _names(session_factory, owner_id) == ["home-lab"]


@pytest.mark.parametrize(
    "name",
    [
        "home lab",
        " home lab ",
        "E2E Dup 2!",
        "-abc",
        "ㄱabc",
        "서버🙂",
        "a" * 64,
        "   ",
        "",
        "1",
        "2024",
    ],
)
async def test_create_server_rejects_invalid_name_and_leaves_no_rows(
    session_factory: Any, name: str
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()

    with pytest.raises(InvalidInputError):
        await w.call(lambda s: s.create_server(owner_id, name))

    assert await _count(session_factory, OnpremServer, owner_id) == 0
    assert await _count(session_factory, Target, owner_id) == 0


async def test_create_server_accepts_63_hangul_name_that_fits_the_column(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    name = "서" * 63

    registration: OnpremServerRegistration = await w.call(lambda s: s.create_server(owner_id, name))

    assert registration.server.name == name
    assert await _names(session_factory, owner_id) == [name]
    with pytest.raises(InvalidInputError):
        await w.call(lambda s: s.create_server(owner_id, name + "서"))


async def test_create_server_names_differing_only_by_case_do_not_conflict(
    session_factory: Any,
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()

    await w.call(lambda s: s.create_server(owner_id, "Home-Lab"))
    await w.call(lambda s: s.create_server(owner_id, "home-lab"))

    assert await _names(session_factory, owner_id) == ["Home-Lab", "home-lab"]
    with pytest.raises(OnpremServerNameConflictError):
        await w.call(lambda s: s.create_server(owner_id, "home-lab"))


@pytest.mark.parametrize("legacy_name", LEGACY_SERVER_NAMES)
async def test_server_with_a_legacy_name_stays_usable_after_the_name_rule(
    session_factory: Any, legacy_name: str
) -> None:
    w = World(session_factory)
    owner_id = await w.owner()
    # 규칙이 생기기 전에 등록한 공백·특수문자·숫자만인 이름. Repository 로 바로 넣는다.
    async with session_factory.begin() as session:
        legacy = await _add_server_row(session, owner_id, name=legacy_name, server_key="aaaaaaaa")

    listed: list[OnpremServer] = await w.call(lambda s: s.search_servers(owner_id))
    fetched: OnpremServer = await w.call(lambda s: s.get_server(owner_id, legacy.id))
    reissued: OnpremServerRegistration = await w.call(
        lambda s: s.reissue_registration_token(owner_id, legacy.id)
    )
    with pytest.raises(InvalidInputError):
        await w.call(lambda s: s.create_server(owner_id, legacy_name))
    await w.call(lambda s: s.delete_server(owner_id, legacy.id))

    assert [server.name for server in listed] == [legacy_name]
    assert fetched.name == reissued.server.name == legacy_name
    assert await w.call(lambda s: s.search_servers(owner_id)) == []


def _devices(key: str) -> list[TailscaleDevice]:
    return [
        TailscaleDevice("d1", f"iris-{key}", f"iris-{key}.t.ts.net", ("tag:iris-onprem",)),
        # 같은 hostname 이지만 서버 태그가 없는 사용자 기기, 다른 서버, 이름이 다른 기기.
        TailscaleDevice("d2", f"iris-{key}", "laptop.t.ts.net", ()),
        TailscaleDevice("d3", "iris-other000", "iris-other000.t.ts.net", ("tag:iris-onprem",)),
        TailscaleDevice("d4", f"iris-{key}-1", "x.t.ts.net", ("tag:iris-onprem",)),
    ]


async def _deleted_registered_server(w: World) -> OnpremServerRegistration:
    owner_id = await w.owner()
    registration = await w.register(owner_id)
    await w.run_next()
    await w.call(lambda s: s.delete_server(owner_id, registration.server.id))
    return registration


async def test_delete_removes_only_the_servers_tagged_tailscale_device(
    session_factory: Any,
) -> None:
    tailscale = FakeTailscale()
    w = World(session_factory, tailscale)
    registration = await _deleted_registered_server(w)
    tailscale.devices = _devices(registration.server.server_key)

    await w.run_next()

    server = await w.load(registration.server.id)
    assert tailscale.deleted == ["d1"]
    assert server.next_check_at is None
    assert f"platform/onprem-servers/{registration.server.server_key}" not in w.head_tree()


async def test_delete_without_tailscale_key_still_finishes_cleanup(session_factory: Any) -> None:
    w = World(session_factory)
    registration = await _deleted_registered_server(w)

    await w.run_next()

    server = await w.load(registration.server.id)
    assert server.next_check_at is None
    assert server.last_error is None


async def test_tailscale_failure_retries_then_deletes_without_new_commit(
    session_factory: Any,
) -> None:
    tailscale = FakeTailscale()
    w = World(session_factory, tailscale)
    registration = await _deleted_registered_server(w)
    tailscale.devices = _devices(registration.server.server_key)
    tailscale.fail = 1

    await w.run_next()
    server = await w.load(registration.server.id)
    assert server.gitops_attempts == 1
    assert server.last_error is not None and "TailscaleApiError" in server.last_error
    assert server.next_check_at is not None
    removal_head = w.gitops.head

    await w.run_next()

    server = await w.load(registration.server.id)
    assert tailscale.deleted == ["d1"]
    assert w.gitops.head == removal_head
    assert server.next_check_at is None


async def test_tailscale_failures_exhausted_give_up_with_error(session_factory: Any) -> None:
    tailscale = FakeTailscale()
    w = World(session_factory, tailscale)
    registration = await _deleted_registered_server(w)
    tailscale.devices = _devices(registration.server.server_key)
    tailscale.fail = MAX_GITOPS_ATTEMPTS

    for _ in range(MAX_GITOPS_ATTEMPTS):
        await w.run_next()

    server = await w.load(registration.server.id)
    assert tailscale.deleted == []
    assert server.next_check_at is None
    assert server.last_error is not None
    assert server.is_deleted


async def test_touch_last_seen_skips_recent_heartbeat(session_factory: Any) -> None:
    w = World(session_factory)
    registration = await w.register(await w.owner())
    server_id = registration.server.id
    first = datetime.now(UTC)
    interval = timedelta(seconds=30)

    async with session_factory.begin() as session:
        await session.execute(update(OnpremServer).values(last_seen_at=None))
    async with session_factory.begin() as session:
        await OnpremServerRepository(session).touch_last_seen(server_id, first, interval)
    async with session_factory.begin() as session:
        await OnpremServerRepository(session).touch_last_seen(
            server_id, first + timedelta(seconds=10), interval
        )
    assert (await w.load(server_id)).last_seen_at == first

    async with session_factory.begin() as session:
        await OnpremServerRepository(session).touch_last_seen(
            server_id, first + timedelta(seconds=31), interval
        )
    assert (await w.load(server_id)).last_seen_at == first + timedelta(seconds=31)


async def test_metric_samples_are_stored_searched_and_pruned(session_factory: Any) -> None:
    async with session_factory.begin() as session:
        service = await seed_service(session)
    start = datetime.now(UTC) - timedelta(days=8)
    recent = datetime.now(UTC) - timedelta(minutes=1)
    async with session_factory.begin() as session:
        await OnpremMetricSampleRepository(session).add_all(
            [
                OnpremMetricSample(
                    service_id=service.id,
                    pod="app-a",
                    collected_at=at,
                    cpu_millicores=12.5,
                    memory_bytes=1024,
                )
                for at in (start, recent)
            ]
        )
    async with session_factory.begin() as session:
        repository = OnpremMetricSampleRepository(session)
        assert len(await repository.search_by_service_id(service.id, start, recent)) == 2
        await repository.delete_collected_before(datetime.now(UTC) - timedelta(days=7))
    async with session_factory() as session:
        found = await OnpremMetricSampleRepository(session).search_by_service_id(
            service.id, start, recent
        )
    assert [(s.pod, s.cpu_millicores, s.memory_bytes) for s in found] == [("app-a", 12.5, 1024)]
