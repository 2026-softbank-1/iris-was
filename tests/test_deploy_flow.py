"""DeployService 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

GitOps 저장소·Argo CD·ECR 은 메모리 대역으로 바꾼다. `alembic upgrade head` 가 끝난 DB 여야 하고
데이터 테이블을 비우므로 전용 DB 를 쓴다.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.argocd_client import ArgoAppStatus
from app.core.config import DeployWorkerSettings
from app.core.exceptions import ExternalError, GitOpsConflictError
from app.enums import (
    Builder,
    BuildStatus,
    DeploymentStatus,
    DeploymentTrigger,
    Environment,
    FailureCode,
    JobKind,
    JobStatus,
    ReleaseStatus,
)
from app.models import Build, DeploymentRequest, Job, Release, Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.job_repository import JobRepository
from app.services.deploy_service import DeployService
from app.services.deployment_request_service import DeploymentRequestService
from tests.worker_support import (
    add,
    requires_database,
    seed_service,
    session_factory_with_clean_data,
)

pytestmark = [pytest.mark.integration, requires_database]

REPOSITORY_URI = "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1"
SETTINGS = DeployWorkerSettings(
    aws_region="ap-northeast-2",
    base_domain="example.app",
    gitops_repository="org/gitops-environments",
    gitops_app_id=1,
    gitops_app_private_key="unused",
    gitops_installation_id=2,
    argocd_server_url="http://argocd.test",
    argocd_token="unused",
)


class FakeGitOps:
    """GitOps 저장소 대역. 커밋은 디렉터리별 tree SHA 만 들고, main 은 fast-forward 만 된다."""

    def __init__(self) -> None:
        self.commits: dict[str, tuple[str | None, dict[str, str]]] = {"c0": (None, {})}
        self.trees: dict[str, dict[str, str]] = {}
        self.head = "c0"
        self.fail_create_tree = False
        self.fail_next_update = False

    async def create_installation_token(
        self, installation_id: int, repository_name: str | None, contents: str = "read"
    ) -> str:
        return "token"

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        return self.head

    async def create_tree(self, token: str, full_name: str, files: dict[str, str]) -> str:
        if self.fail_create_tree:
            raise ExternalError("github down")
        sha = f"t{len(self.trees)}"
        self.trees[sha] = files
        return sha

    async def create_commit(
        self, token: str, full_name: str, parent_sha: str, path: str, tree_sha: str, message: str
    ) -> str:
        sha = f"c{len(self.commits)}"
        self.commits[sha] = (parent_sha, {**self.commits[parent_sha][1], path: tree_sha})
        return sha

    async def update_branch(self, token: str, full_name: str, branch: str, commit_sha: str) -> None:
        if self.fail_next_update:
            self.fail_next_update = False
            raise ExternalError("github down")
        if self.commits[commit_sha][0] != self.head:
            raise GitOpsConflictError("branch moved")
        self.head = commit_sha

    async def find_subtree_sha(
        self, token: str, full_name: str, commit_sha: str, path: str
    ) -> str | None:
        return self.commits[commit_sha][1].get(path)

    async def contains(self, token: str, full_name: str, commit_sha: str, head_sha: str) -> bool:
        sha: str | None = head_sha
        while sha is not None:
            if sha == commit_sha:
                return True
            sha = self.commits[sha][0]
        return False

    async def push_foreign(self, path: str) -> None:
        """다른 주체가 path 를 바꾼 커밋을 main 에 올린다."""
        tree_sha = await self.create_tree("", "", {"x.json": "{}"})
        self.head = await self.create_commit("", "", self.head, path, tree_sha, "foreign")


class FakeArgo:
    def __init__(self) -> None:
        self.status: ArgoAppStatus | None = None

    async def get_application(self, name: str, refresh: bool = False) -> ArgoAppStatus | None:
        return self.status


class FakeEcr:
    def __init__(self) -> None:
        self.tags: list[str] = []
        self.fail = False

    async def tag_image(self, repository_name: str, image_digest: str, image_tag: str) -> None:
        if self.fail:
            raise ExternalError("ecr down")
        self.tags.append(image_tag)


def _argo(revision: str, health: str = "Healthy", phase: str = "Succeeded") -> ArgoAppStatus:
    return ArgoAppStatus("Synced", revision, health, phase, revision, None)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


class Harness:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.gitops = FakeGitOps()
        self.argo = FakeArgo()
        self.ecr = FakeEcr()
        self.service = DeployService(
            session_factory,
            self.gitops,  # type: ignore[arg-type]
            self.argo,  # type: ignore[arg-type]
            self.ecr,  # type: ignore[arg-type]
            SETTINGS,
            "worker-1",
        )
        self.service_id: int | None = None

    async def request_deploy(self, *, max_attempts: int = 3, cancelled: bool = False) -> int:
        """같은 서비스에 빌드가 끝난 배포 요청과 DEPLOY job 을 만든다. 요청 ID 를 돌려준다."""
        async with self.session_factory.begin() as session:
            if self.service_id is None:
                self.service_id = (await seed_service(session)).id
            unique = uuid4().hex
            request = await add(
                session,
                DeploymentRequest(
                    service_id=self.service_id,
                    environment=Environment.PROD,
                    source_sha="a" * 40,
                    trigger_type=DeploymentTrigger.MANUAL,
                    idempotency_key=unique,
                    status=DeploymentStatus.DEPLOYING,
                ),
            )
            build = await add(
                session,
                Build(
                    deployment_request_id=request.id,
                    status=BuildStatus.SUCCEEDED,
                    builder=Builder.DOCKERFILE,
                    source_sha="a" * 40,
                    image_repository=REPOSITORY_URI,
                    image_tag="b-1",
                    image_digest=f"sha256:{unique}",
                    deploy_config={"healthcheckTimeout": 60},
                ),
            )
            session.add(
                Job(
                    deployment_request_id=request.id,
                    kind=JobKind.DEPLOY,
                    payload={"build_id": build.id},
                    max_attempts=max_attempts,
                )
            )
            if cancelled:
                await session.execute(
                    text("UPDATE deployment_requests SET cancel_requested_at = now()")
                )
        return request.id

    async def run_next(self, kind: JobKind) -> Job:
        """kind 의 다음 job 을 snooze 와 무관하게 바로 선점해 한 번 처리한다."""
        async with self.session_factory.begin() as session:
            await session.execute(
                update(Job).where(Job.kind == kind).values(run_after=text("now()"))
            )
            job = await JobRepository(session).claim_next_job("worker-1", frozenset({kind}), 0)
        assert job is not None
        try:
            await self.service.run(job)
        except Exception as exc:
            await self.service.retry_or_fail(job, exc)
        return job

    async def deploy_successfully(self) -> int:
        request_id = await self.request_deploy()
        await self.run_next(JobKind.DEPLOY)
        self.argo.status = _argo(self.gitops.head)
        await self.run_next(JobKind.RECONCILE)
        return request_id

    async def load(self, request_id: int) -> tuple[DeploymentRequest, Release | None, list[Job]]:
        async with self.session_factory() as session:
            request = await session.get_one(DeploymentRequest, request_id)
            release = await session.scalar(
                select(Release).where(Release.deployment_request_id == request_id)
            )
            jobs = list(
                await session.scalars(
                    select(Job).where(Job.deployment_request_id == request_id).order_by(Job.id)
                )
            )
        return request, release, jobs


def _job_states(jobs: list[Job]) -> list[tuple[JobKind, JobStatus]]:
    return [(job.kind, job.status) for job in jobs]


async def test_deploy_and_reconcile_success_marks_release_succeeded(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()

    await h.run_next(JobKind.DEPLOY)
    _, release, _ = await h.load(request_id)
    assert release is not None and release.gitops_commit_sha == h.gitops.head
    tree_sha = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    values = json.loads(h.gitops.trees[tree_sha]["values.yaml"])
    assert values["image"]["digest"] == release.image_digest
    assert values["release"]["id"] == release.id

    h.argo.status = _argo(h.gitops.head)
    await h.run_next(JobKind.RECONCILE)

    request, release, jobs = await h.load(request_id)
    assert release is not None
    assert (release.status, request.status) == (ReleaseStatus.SUCCEEDED, DeploymentStatus.SUCCEEDED)
    assert h.ecr.tags == [f"r-{release.id}"]
    assert _job_states(jobs) == [
        (JobKind.DEPLOY, JobStatus.SUCCEEDED),
        (JobKind.RECONCILE, JobStatus.SUCCEEDED),
    ]


async def test_reconcile_image_tag_failure_still_succeeds(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.ecr.fail = True
    h.argo.status = _argo(h.gitops.head)

    await h.run_next(JobKind.RECONCILE)

    request, release, jobs = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.SUCCEEDED
    assert request.status == DeploymentStatus.SUCCEEDED
    assert jobs[-1].status == JobStatus.SUCCEEDED


async def test_reconcile_argo_behind_snoozes_without_attempt(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.argo.status = _argo("c0")

    for _ in range(4):
        await h.run_next(JobKind.RECONCILE)

    _, release, jobs = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.PENDING
    assert (jobs[1].status, jobs[1].attempts) == (JobStatus.QUEUED, 0)


async def test_deploy_cancel_requested_supersedes_without_release(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy(cancelled=True)

    await h.run_next(JobKind.DEPLOY)

    request, release, jobs = await h.load(request_id)
    assert (request.status, release) == (DeploymentStatus.SUPERSEDED, None)
    assert h.gitops.head == "c0"
    assert _job_states(jobs) == [(JobKind.DEPLOY, JobStatus.SUCCEEDED)]


async def test_deploy_release_in_flight_snoozes(session_factory: Any) -> None:
    h = Harness(session_factory)
    first_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    # 첫 요청은 실패했지만 되돌림이 끝나지 않아 release 가 진행 중이다. 요청이 FAILED 라
    # 새 요청은 만들어지고, release 가 끝날 때까지 새 배포가 기다려야 한다.
    async with session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_requests SET status = 'FAILED', failure_code = 'DEPLOY_FAILED'"
                " WHERE id = :id"
            ),
            {"id": first_id},
        )
    second_id = await h.request_deploy()

    await h.run_next(JobKind.DEPLOY)

    _, release, jobs = await h.load(second_id)
    assert release is None
    assert (jobs[0].status, jobs[0].attempts) == (JobStatus.QUEUED, 0)


async def test_deploy_crash_after_commit_resumes_without_new_commit(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    h.gitops.fail_next_update = True

    await h.run_next(JobKind.DEPLOY)
    _, release, jobs = await h.load(request_id)
    assert release is not None and release.gitops_commit_sha is not None
    assert jobs[0].status == JobStatus.RETRY_WAIT
    commit_count = len(h.gitops.commits)

    await h.run_next(JobKind.DEPLOY)

    _, release, jobs = await h.load(request_id)
    assert release is not None and h.gitops.head == release.gitops_commit_sha
    assert len(h.gitops.commits) == commit_count
    assert _job_states(jobs) == [
        (JobKind.DEPLOY, JobStatus.SUCCEEDED),
        (JobKind.RECONCILE, JobStatus.QUEUED),
    ]


async def test_deploy_branch_moved_recommits_on_new_head(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    h.gitops.fail_next_update = True
    await h.run_next(JobKind.DEPLOY)
    await h.gitops.push_foreign("services/999/prod")

    await h.run_next(JobKind.DEPLOY)

    _, release, _ = await h.load(request_id)
    assert release is not None and release.gitops_commit_sha == h.gitops.head
    assert h.gitops.commits[h.gitops.head][1].keys() >= {
        f"services/{h.service_id}/prod",
        "services/999/prod",
    }


async def test_deploy_error_before_commit_exhausted_fails(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy(max_attempts=1)
    h.gitops.fail_create_tree = True

    await h.run_next(JobKind.DEPLOY)

    request, release, jobs = await h.load(request_id)
    assert release is not None
    assert (release.status, request.status, request.failure_code) == (
        ReleaseStatus.FAILED,
        DeploymentStatus.FAILED,
        FailureCode.DEPLOY_INFRA_ERROR,
    )
    assert h.gitops.head == "c0"
    assert _job_states(jobs) == [(JobKind.DEPLOY, JobStatus.FAILED)]


async def test_reconcile_first_deploy_failure_fails_without_rollback(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.argo.status = _argo(h.gitops.head, health="Degraded")

    await h.run_next(JobKind.RECONCILE)

    request, release, jobs = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.FAILED
    assert (request.status, request.failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.DEPLOY_FAILED,
    )
    assert JobKind.ROLLBACK not in [job.kind for job in jobs]


async def test_reconcile_deadline_passed_times_out(session_factory: Any) -> None:
    h = Harness(session_factory)
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    async with session_factory.begin() as session:
        await session.execute(text("UPDATE releases SET deadline_at = now() - interval '1 second'"))

    await h.run_next(JobKind.RECONCILE)

    request, _, _ = await h.load(request_id)
    assert request.failure_code == FailureCode.DEPLOY_TIMED_OUT


async def test_failed_deploy_rolls_back_to_last_known_good(session_factory: Any) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    good_tree = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.argo.status = _argo(h.gitops.head, phase="Failed")

    await h.run_next(JobKind.RECONCILE)
    await h.run_next(JobKind.ROLLBACK)

    _, release, _ = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.ROLLING_BACK
    assert release.revert_commit_sha == h.gitops.head
    assert h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"] == good_tree

    h.argo.status = _argo(h.gitops.head)
    await h.run_next(JobKind.RECONCILE)

    request, release, jobs = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.ROLLED_BACK
    assert (request.status, request.failure_code) == (
        DeploymentStatus.ROLLED_BACK,
        FailureCode.DEPLOY_FAILED,
    )
    assert [job.kind for job in jobs] == [
        JobKind.DEPLOY,
        JobKind.RECONCILE,
        JobKind.ROLLBACK,
        JobKind.RECONCILE,
    ]
    assert len(h.ecr.tags) == 1


async def test_rollback_head_changed_needs_manual_intervention(session_factory: Any) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.argo.status = _argo(h.gitops.head, health="Degraded")
    await h.run_next(JobKind.RECONCILE)
    await h.gitops.push_foreign(f"services/{h.service_id}/prod")
    head_before = h.gitops.head

    await h.run_next(JobKind.ROLLBACK)

    request, release, jobs = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.FAILED
    assert request.status == DeploymentStatus.MANUAL_INTERVENTION
    assert jobs[-1].status == JobStatus.MANUAL_INTERVENTION
    assert h.gitops.head == head_before


async def test_failed_rollback_needs_manual_intervention(session_factory: Any) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    request_id = await h.request_deploy()
    await h.run_next(JobKind.DEPLOY)
    h.argo.status = _argo(h.gitops.head, health="Degraded")
    await h.run_next(JobKind.RECONCILE)
    await h.run_next(JobKind.ROLLBACK)
    h.argo.status = _argo(h.gitops.head, health="Degraded")

    await h.run_next(JobKind.RECONCILE)

    request, release, _ = await h.load(request_id)
    assert release is not None and release.status == ReleaseStatus.FAILED
    assert request.status == DeploymentStatus.MANUAL_INTERVENTION


async def _request_reusing_image(
    h: Harness, source_request_id: int, trigger_type: DeploymentTrigger
) -> int:
    """실제 Repository 로 빌드 없는 배포 요청(롤백·재시작)을 만든다. 요청 ID 를 돌려준다."""
    async with h.session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        source = await session.get_one(DeploymentRequest, source_request_id)
        source_build = await BuildRepository(session).find_by_deployment_request_id(source.id)
        assert source_build is not None
        request = await DeploymentRequestService(
            DeploymentRequestRepository(session),
            JobRepository(session),
            DeploymentStatusHistoryRepository(session),
            BuildRepository(session),
        ).create_deployment_request_reusing_image(
            service,
            source_deployment_request=source,
            source_build=source_build,
            trigger_type=trigger_type,
            idempotency_key=f"reuse-{uuid4().hex}",
        )
        assert request is not None
        return request.id


@pytest.mark.parametrize("trigger_type", [DeploymentTrigger.ROLLBACK, DeploymentTrigger.RESTART])
async def test_reused_image_request_deploys_source_digest_as_new_release(
    session_factory: Any, trigger_type: DeploymentTrigger
) -> None:
    h = Harness(session_factory)
    v1_id = await h.deploy_successfully()
    v2_id = await h.deploy_successfully()
    source_id = v1_id if trigger_type == DeploymentTrigger.ROLLBACK else v2_id
    source_request, source_release, _ = await h.load(source_id)
    _, v2_release, _ = await h.load(v2_id)
    assert source_release is not None and v2_release is not None
    commits_before = len(h.gitops.commits)

    request_id = await _request_reusing_image(h, source_id, trigger_type)
    await h.run_next(JobKind.DEPLOY)

    request, release, _ = await h.load(request_id)
    assert release is not None
    assert (request.trigger_type, request.status) == (trigger_type, DeploymentStatus.DEPLOYING)
    assert release.image_digest == source_release.image_digest
    assert release.previous_good_release_id == v2_release.id
    assert len(h.gitops.commits) == commits_before + 1
    tree_sha = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    values = json.loads(h.gitops.trees[tree_sha]["values.yaml"])
    assert values["image"]["digest"] == source_release.image_digest
    # release id 가 바뀌어야 digest 가 같아도(재시작) Pod 가 새로 뜬다.
    assert values["release"]["id"] == release.id
    assert release.id not in (source_release.id, v2_release.id)

    h.argo.status = _argo(h.gitops.head)
    await h.run_next(JobKind.RECONCILE)

    request, release, _ = await h.load(request_id)
    assert release is not None
    assert (release.status, request.status) == (ReleaseStatus.SUCCEEDED, DeploymentStatus.SUCCEEDED)
    assert request.source_deployment_request_id == source_request.id
