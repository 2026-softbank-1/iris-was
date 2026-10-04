"""스택(멀티 이미지 반복 운영) 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

실제 Control API(라우터·DI)와 실제 분석기 subprocess, 실제 DB 를 쓰고 GitHub 만 가짜다.
분석 접수 → Build Worker 분석 → apply(DB 서비스 + 앱 + 참조 변수 + 별칭 + 스택) → 의존 순서 배포
(DB 성공 뒤 앱 시작, 실패 시 보류) → push(경로가 바뀐 앱만 순서대로 + 재분석 → pendingChanges)
→ 증분 apply(중복 없음) → 환경변수 검증(422 VARIABLES_INVALID) → on-prem 거절 → 비밀 비노출.
"""

import hashlib
import hmac
import io
import json
import logging
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_gate_client import SubprocessAnalysisGateClient
from app.clients.source_repository_client import BranchInfo, CommitInfo
from app.core.config import BuildWorkerSettings, Settings, get_settings
from app.core.crypto import VariableCipher
from app.dependencies import (
    SessionDep,
    get_current_user,
    get_session,
    get_source_repository_service,
)
from app.enums import (
    Builder,
    DeploymentStatus,
    DeploymentTrigger,
    FailureCode,
    JobKind,
    OnpremServerStatus,
    RepositoryAnalysisStatus,
    ServiceKind,
    StackDeploymentStepStatus,
    TargetKind,
)
from app.main import app
from app.models import (
    Build,
    DeploymentRequest,
    GithubInstallation,
    Job,
    OnpremServer,
    Project,
    RepositoryAnalysis,
    Service,
    ServiceStack,
    ServiceVariable,
    StackDeploymentStep,
    Target,
    User,
    UserGithubInstallation,
)
from app.models.base import now_utc
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.services.analysis_gate_service import AnalysisGateService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import FakeSourceRepositoryClient, make_repository
from tests.worker_support import add, requires_database, session_factory_with_clean_data

pytestmark = [pytest.mark.integration, requires_database]

SHA = "a" * 40
SHA2 = "b" * 40
REPOSITORY = "owner/shop"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
KEY = Fernet.generate_key().decode()
WEBHOOK_SECRET = "whsec-test"
WORKER_SETTINGS = BuildWorkerSettings(
    github_app_id=1,
    github_app_private_key="unused",
    aws_region="ap-northeast-2",
    codebuild_project="iris-test-build",
    artifact_bucket="iris-test-artifacts",
    analysis_gate_timeout_seconds=60,
)

SHOP_FILES: dict[str, bytes] = {
    "compose.yaml": b"""services:
  web:
    build: ./web
    ports: ["8080:80"]
    depends_on: [api]
  api:
    build: ./api
    ports: ["3000:3000"]
    environment:
      DATABASE_URL: postgres://shop:secret@postgres:5432/shop
      REDIS_URL: redis://redis:6379
      API_TOKEN: ${API_TOKEN}
    depends_on: [postgres, redis]
  worker:
    build: ./worker
    environment:
      REDIS_URL: redis://redis:6379
    depends_on: [redis]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: shop
      POSTGRES_USER: shop
      POSTGRES_PASSWORD: secret
  redis:
    image: redis:7-alpine
""",
    "web/Dockerfile": b"FROM nginx:alpine\nCOPY nginx/default.conf /etc/nginx/conf.d/\nEXPOSE 80\n",
    "web/nginx/default.conf": (
        b"server {\n    listen 80;\n    location /api/ {\n        proxy_pass http://api:3000;\n"
        b"    }\n}\n"
    ),
    "api/Dockerfile": b'FROM node:20-alpine\nCOPY . .\nEXPOSE 3000\nCMD ["node","index.js"]\n',
    "api/package.json": b'{"name":"api"}\n',
    "worker/Dockerfile": b'FROM python:3.12-slim\nCOPY . .\nCMD ["python","worker.py"]\n',
    "worker/requirements.txt": b"redis==5\n",
}
# 새 unit(admin)이 생긴 커밋.
SHOP_FILES_V2 = {
    **SHOP_FILES,
    "compose.yaml": SHOP_FILES["compose.yaml"].replace(
        b"  postgres:\n",
        b'  admin:\n    build: ./admin\n    ports: ["4000:4000"]\n  postgres:\n',
    ),
    "admin/Dockerfile": b'FROM node:20-alpine\nCOPY . .\nEXPOSE 4000\nCMD ["node","a.js"]\n',
    "admin/package.json": b'{"name":"admin"}\n',
}


class FakeGitHub:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def create_installation_token(self, installation_id: int, name: str | None) -> str:
        return "token"

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        return SHA

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int
    ) -> None:
        with tarfile.open(dest, "w:gz") as archive:
            for name, content in self.files.items():
                info = tarfile.TarInfo(f"owner-shop-{sha[:7]}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))


class World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.github_api = FakeSourceRepositoryClient({700001: [make_repository(REPOSITORY)]})
        self.github_api.branches[REPOSITORY] = [BranchInfo("main", True)]
        self.github_api.heads[(REPOSITORY, "main")] = CommitInfo(SHA, "init")
        self.settings = Settings(
            database_url="postgresql+asyncpg://unused/unused",
            project_networking_enabled=True,
            variables_encryption_key=KEY,
            github_webhook_secret=WEBHOOK_SECRET,
        )
        self.cipher = VariableCipher(KEY)
        self.user: User
        self.project_id: int
        self.texts: list[str] = []

    async def seed(self) -> None:
        async with self.factory.begin() as session:
            self.user = await add(session, User(github_id=1, login="owner"))
            installation = await add(
                session,
                GithubInstallation(
                    installation_id=700001, account_login="owner", account_type="User"
                ),
            )
            await add(
                session,
                UserGithubInstallation(
                    user_id=self.user.id, github_installation_id=installation.id
                ),
            )
            self.project_id = (await add(session, Project(name="shop", owner_id=self.user.id))).id


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


@pytest.fixture
async def world(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[World]:
    w = World(session_factory)
    await w.seed()

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    def source_override(session: SessionDep) -> SourceRepositoryService:
        return SourceRepositoryService(
            GithubInstallationRepository(session),
            w.github_api,  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: w.settings
    app.dependency_overrides[get_current_user] = lambda: w.user
    app.dependency_overrides[get_source_repository_service] = source_override
    yield w
    app.dependency_overrides.clear()


@pytest.fixture
async def client(world: World) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


# --- 도우미


async def _call(world: World, client: AsyncClient, method: str, url: str, **kw: Any) -> Any:
    response = await client.request(method, url, **kw)
    world.texts.append(response.text)
    return response


async def _analyze(world: World, client: AsyncClient, files: dict[str, bytes]) -> int:
    response = await _call(
        world,
        client,
        "POST",
        f"/api/v1/projects/{world.project_id}/repository-analyses",
        json={"sourceRepositoryUrl": REPOSITORY_URL, "sourceBranch": "main"},
    )
    assert response.status_code == 202, response.text
    analysis_id = int(response.json()["data"]["id"])
    await _run_analyses(world, files)
    return analysis_id


async def _run_analyses(world: World, files: dict[str, bytes]) -> None:
    worker = AnalysisGateService(
        world.factory,
        FakeGitHub(files),  # type: ignore[arg-type]
        SubprocessAnalysisGateClient(WORKER_SETTINGS.analysis_gate_command, timeout_seconds=60),
        WORKER_SETTINGS,
        "worker-1",
    )
    while (analysis := await worker.claim_next_analysis()) is not None:
        await worker.run(analysis)


async def _apply(
    world: World, client: AsyncClient, analysis_id: int, units: list[str], **body: Any
) -> Any:
    return await _call(
        world,
        client,
        "POST",
        f"/api/v1/projects/{world.project_id}/repository-analyses/{analysis_id}/apply",
        json={"units": [{"unitId": u} for u in units], "deploy": True, **body},
    )


async def _services(world: World) -> dict[str, Service]:
    async with world.factory() as session:
        rows = (await session.scalars(select(Service).where(Service.is_deleted.is_(False)))).all()
    return {s.stack_unit_id or s.name: s for s in rows}


async def _latest_requests(world: World) -> dict[str, DeploymentRequest]:
    services = await _services(world)
    by_id = {s.id: unit for unit, s in services.items()}
    async with world.factory() as session:
        rows = (
            await session.scalars(select(DeploymentRequest).order_by(DeploymentRequest.id))
        ).all()
    return {by_id[r.service_id]: r for r in rows if r.service_id in by_id}


async def _jobs(world: World, request_id: int) -> list[JobKind]:
    async with world.factory() as session:
        rows = await session.scalars(
            select(Job.kind).where(Job.deployment_request_id == request_id).order_by(Job.id)
        )
        return list(rows.all())


async def _move(world: World, request_id: int, *statuses: DeploymentStatus, **kw: Any) -> None:
    """Worker 가 하는 상태 전이를 같은 경로(DeploymentStatusService)로 흉내 낸다."""
    for status in statuses:
        async with world.factory.begin() as session:
            await DeploymentStatusService.create(session).transition_status(
                request_id,
                status,
                failure_code=kw.get("failure_code") if status == DeploymentStatus.FAILED else None,
            )


async def _succeed(world: World, request: DeploymentRequest) -> None:
    async with world.factory() as session:
        current = await session.get_one(DeploymentRequest, request.id)
    path = {
        DeploymentStatus.QUEUED: [
            DeploymentStatus.BUILDING,
            DeploymentStatus.DEPLOYING,
            DeploymentStatus.SUCCEEDED,
        ],
        DeploymentStatus.BUILDING: [DeploymentStatus.DEPLOYING, DeploymentStatus.SUCCEEDED],
        DeploymentStatus.DEPLOYING: [DeploymentStatus.SUCCEEDED],
    }[current.status]
    await _move(world, request.id, *path)


async def _set_variable(
    world: World, client: AsyncClient, service_id: int, key: str, value: str
) -> None:
    response = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{service_id}/variables",
        json={"key": key, "value": value},
    )
    assert response.status_code == 201, response.text


async def _applied_shop(world: World, client: AsyncClient) -> dict[str, Any]:
    """apply 한 번(환경변수 부족으로 배포 보류) → API_TOKEN 추가 → 같은 apply 재전송(배포 접수)."""
    analysis_id = await _analyze(world, client, SHOP_FILES)
    first = await _apply(world, client, analysis_id, ["web", "api", "worker"])
    assert first.status_code == 201, first.text
    services = await _services(world)
    await _set_variable(world, client, services["api"].id, "API_TOKEN", "jwt-value")
    second = await _apply(world, client, analysis_id, ["web"])
    assert second.status_code == 201, second.text
    return {
        "analysis_id": analysis_id,
        "first": first.json()["data"],
        "second": second.json()["data"],
    }


async def _stack(world: World, client: AsyncClient, stack_id: int) -> dict[str, Any]:
    response = await _call(
        world, client, "GET", f"/api/v1/projects/{world.project_id}/stacks/{stack_id}"
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _passwords(world: World) -> list[str]:
    """DB 서비스가 만든 비밀번호 평문. 응답·로그에 나오면 안 된다."""
    async with world.factory() as session:
        rows = (
            await session.scalars(
                select(ServiceVariable).where(ServiceVariable.key.like("%PASSWORD"))
            )
        ).all()
    return [world.cipher.decrypt(v.encrypted_value) for v in rows if v.encrypted_value]


# --- 테스트


async def test_apply_creates_databases_apps_references_aliases_and_ordered_stack_deployment(
    world: World, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    applied = await _applied_shop(world, client)
    first, second = applied["first"], applied["second"]

    # 첫 apply: 서비스는 만들고, api 의 필수 변수(API_TOKEN)가 없어 배포는 보류했다.
    assert sorted(s["stack"]["unitId"] for s in first["services"]) == ["api", "web", "worker"]
    assert sorted(d["stack"]["unitId"] for d in first["databases"]) == ["postgres", "redis"]
    assert "stackDeploymentId" not in first
    issues = {i["serviceId"]: i for i in first["variableIssues"]}
    services = await _services(world)
    api_issue = issues[services["api"].id]["issues"]
    assert [(i["key"], i["code"], i["severity"]) for i in api_issue] == [
        ("API_TOKEN", "REQUIRED_MISSING", "error")
    ]
    assert second["stackDeploymentId"] > 0 and "variableIssues" not in second
    assert {s["id"] for s in second["services"]} == {s["id"] for s in first["services"]}

    # DB 서비스: 고정 이미지, 엔진별 자격 증명 변수(암호화), 소스 없음.
    pg, redis = services["postgres"], services["redis"]
    assert (pg.kind, pg.database_engine, redis.database_engine) == (
        ServiceKind.DATABASE,
        "postgres",
        "redis",
    )
    assert pg.database_config is not None
    assert pg.database_config["image"].startswith("docker.io/library/postgres:16-alpine@sha256:")
    assert (pg.database_config["user"], pg.database_config["database"]) == ("shop", "shop")
    assert pg.github_installation_id is None and pg.is_auto_deploy is False
    async with world.factory() as session:
        variables = (await session.scalars(select(ServiceVariable))).all()
    by_service: dict[int, dict[str, ServiceVariable]] = {}
    for v in variables:
        by_service.setdefault(v.service_id, {})[v.key] = v
    assert set(by_service[pg.id]) == {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    assert set(by_service[redis.id]) == {"REDIS_PASSWORD"}

    # 참조 변수: env binding → 같은 스택 DB 의 url. 값은 담지 않는다.
    api, worker, web = services["api"], services["worker"], services["web"]
    # url 은 코드가 쓴 스킴과 경로(쿼리)를 함께 담는다. 자격 증명은 담지 않는다.
    assert by_service[api.id]["DATABASE_URL"].reference == {
        "serviceId": pg.id,
        "property": "url",
        "scheme": "postgres",
        "suffix": "/shop",
    }
    assert by_service[api.id]["REDIS_URL"].reference == {
        "serviceId": redis.id,
        "property": "url",
        "scheme": "redis",
    }
    assert by_service[worker.id]["REDIS_URL"].reference == {
        "serviceId": redis.id,
        "property": "url",
        "scheme": "redis",
    }
    assert by_service[api.id]["DATABASE_URL"].encrypted_value is None
    # 호스트 별칭: compose 호스트명 → 대상 서비스.
    assert {a["name"]: a["targetServiceId"] for a in api.host_aliases or []} == {
        "postgres": pg.id,
        "redis": redis.id,
    }
    assert web.host_aliases == [{"name": "api", "targetServiceId": api.id, "port": 3000}]

    # 의존 순서: DB 둘만 시작(DEPLOY job), 앱은 QUEUED 로 기다린다(job 없음).
    requests = await _latest_requests(world)
    assert requests["postgres"].status == DeploymentStatus.DEPLOYING
    assert await _jobs(world, requests["postgres"].id) == [JobKind.DEPLOY]
    assert requests["postgres"].source_sha.startswith("image-")
    for unit in ("api", "worker", "web"):
        assert requests[unit].status == DeploymentStatus.QUEUED
        assert await _jobs(world, requests[unit].id) == []
        assert requests[unit].source_sha == SHA
    async with world.factory() as session:
        steps = {
            s.service_id: s for s in (await session.scalars(select(StackDeploymentStep))).all()
        }
    assert {unit: steps[services[unit].id].step_order for unit in services} == {
        "postgres": 1,
        "redis": 1,
        "api": 2,
        "worker": 2,
        "web": 3,
    }
    stack = await _stack(world, client, api.stack_id or 0)
    view = {s["unitId"]: s for s in stack["services"]}
    assert [s["unitId"] for s in stack["services"]][:2] == ["postgres", "redis"]
    assert view["api"]["status"] == "QUEUED"
    assert view["api"]["waitingFor"] == ["postgres", "redis"]
    assert view["web"]["dependsOn"] == ["api"] and view["web"]["order"] == 3
    assert stack["isDeploying"] is True

    # redis 성공 → redis 만 기다리던 worker 시작. api 는 postgres 를 더 기다린다.
    await _succeed(world, requests["redis"])
    assert await _jobs(world, requests["worker"].id) == [JobKind.BUILD]
    assert await _jobs(world, requests["api"].id) == []
    # postgres 성공 → api 시작. web 은 api 를 기다린다.
    await _succeed(world, requests["postgres"])
    assert await _jobs(world, requests["api"].id) == [JobKind.BUILD]
    assert await _jobs(world, requests["web"].id) == []
    await _succeed(world, requests["api"])
    assert await _jobs(world, requests["web"].id) == [JobKind.BUILD]

    # 비밀번호 평문은 어떤 응답에도, 로그에도 없다.
    passwords = await _passwords(world)
    assert len(passwords) == 2
    db_vars = await _call(world, client, "GET", f"/api/v1/services/{pg.id}/variables")
    assert db_vars.json()["data"]["variables"] == []
    system = {v["key"]: v.get("value") for v in db_vars.json()["data"]["systemVariables"]}
    assert system == {"POSTGRES_USER": "shop", "POSTGRES_DB": "shop", "POSTGRES_PASSWORD": None}
    api_vars = await _call(world, client, "GET", f"/api/v1/services/{api.id}/variables")
    database_url = next(
        v for v in api_vars.json()["data"]["variables"] if v["key"] == "DATABASE_URL"
    )
    assert database_url["resolved"] == (
        f"postgres://shop:****@app.svc-{pg.id}.svc.cluster.local:5432/shop"
    )
    assert "value" not in database_url
    port_variable = next(
        v for v in api_vars.json()["data"]["systemVariables"] if v["key"] == "PORT"
    )
    assert port_variable["value"] == "3000"
    detail = await _call(world, client, "GET", f"/api/v1/services/{pg.id}")
    data = detail.json()["data"]
    assert data["connection"]["urlTemplate"] == (
        f"postgresql://shop:****@app.svc-{pg.id}.svc.cluster.local:5432/shop"
    )
    assert (data["kind"], data["internalPort"], data["database"]["storageGi"]) == (
        "DATABASE",
        5432,
        5,
    )
    for password in passwords:
        assert all(password not in text for text in world.texts)
        assert password not in caplog.text


async def test_failed_database_holds_dependent_apps(world: World, client: AsyncClient) -> None:
    applied = await _applied_shop(world, client)
    requests = await _latest_requests(world)

    await _move(
        world,
        requests["postgres"].id,
        DeploymentStatus.FAILED,
        failure_code=FailureCode.DEPLOY_FAILED,
    )

    after = await _latest_requests(world)
    # api 는 postgres 를 기다리다 보류, web 은 api 의 보류로 함께 보류. worker 는 redis 만 기다린다.
    assert (after["api"].status, after["api"].failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.DEPENDENCY_FAILED,
    )
    assert (after["web"].status, after["web"].failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.DEPENDENCY_FAILED,
    )
    assert after["worker"].status == DeploymentStatus.QUEUED
    assert await _jobs(world, after["api"].id) == [] and await _jobs(world, after["web"].id) == []
    async with world.factory() as session:
        builds = {
            b.deployment_request_id: b.status for b in (await session.scalars(select(Build))).all()
        }
    assert builds[after["api"].id] == "CANCELLED"
    services = await _services(world)
    stack = await _stack(world, client, services["api"].stack_id or 0)
    view = {s["unitId"]: s for s in stack["services"]}
    assert (view["api"]["status"], view["api"]["heldBy"]) == ("HELD", "postgres")
    assert (view["web"]["status"], view["web"]["heldBy"]) == ("HELD", "api")
    assert view["postgres"]["failureCode"] == "DEPLOY_FAILED"

    # redis 는 그대로 진행되고 worker 가 시작한다.
    await _succeed(world, requests["redis"])
    assert await _jobs(world, after["worker"].id) == [JobKind.BUILD]

    # 스택 재배포: 실패한 postgres(떠 있지 않음)부터 다시. redis 는 이미 떠 있어 다시 띄우지 않는다.
    await _succeed(world, after["worker"])
    response = await _call(
        world,
        client,
        "POST",
        f"/api/v1/projects/{world.project_id}/stacks/{services['api'].stack_id}/deployments",
        json={},
        headers={"Idempotency-Key": "redeploy-1"},
    )
    assert response.status_code == 202, response.text
    again = await _latest_requests(world)
    assert again["redis"].id == requests["redis"].id
    assert again["postgres"].id != requests["postgres"].id
    assert again["postgres"].trigger_type == DeploymentTrigger.MANUAL
    assert again["api"].status == DeploymentStatus.QUEUED
    assert await _jobs(world, again["postgres"].id) == [JobKind.DEPLOY]
    assert await _jobs(world, again["api"].id) == []
    # 같은 키로 다시 보내면 새로 만들지 않는다. 진행 중이면 다른 키는 409.
    replay = await _call(
        world,
        client,
        "POST",
        f"/api/v1/projects/{world.project_id}/stacks/{services['api'].stack_id}/deployments",
        json={},
        headers={"Idempotency-Key": "redeploy-1"},
    )
    assert replay.status_code == 202
    assert (await _latest_requests(world))["postgres"].id == again["postgres"].id
    busy = await _call(
        world,
        client,
        "POST",
        f"/api/v1/projects/{world.project_id}/stacks/{services['api'].stack_id}/deployments",
        json={},
    )
    assert busy.status_code == 409 and busy.json()["code"] == "DEPLOYMENT_IN_PROGRESS"
    assert applied["second"]["stackDeploymentId"] > 0


def _signed(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": signature, "X-GitHub-Event": "push"}


def _push(delivery: str, modified: list[str], sha: str = SHA2) -> tuple[bytes, dict[str, str]]:
    raw, headers = _signed(
        {
            "ref": "refs/heads/main",
            "after": sha,
            "deleted": False,
            "repository": {"html_url": REPOSITORY_URL, "full_name": REPOSITORY},
            "head_commit": {"id": sha, "message": "change"},
            "commits": [{"id": sha, "added": [], "modified": modified, "removed": []}],
        }
    )
    return raw, {**headers, "X-GitHub-Delivery": delivery}


async def _finish_all(world: World) -> None:
    for _ in range(4):
        for request in (await _latest_requests(world)).values():
            if request.status in (
                DeploymentStatus.QUEUED,
                DeploymentStatus.BUILDING,
                DeploymentStatus.DEPLOYING,
            ) and await _jobs(world, request.id):
                await _succeed(world, request)


async def test_push_rebuilds_changed_apps_in_order_reanalyzes_and_incremental_apply(
    world: World, client: AsyncClient
) -> None:
    applied = await _applied_shop(world, client)
    await _finish_all(world)
    before = await _latest_requests(world)
    assert all(r.status == DeploymentStatus.SUCCEEDED for r in before.values())
    services = await _services(world)
    stack_id = services["api"].stack_id

    raw, headers = _push("d-1", ["api/index.js", "web/nginx/default.conf"])
    response = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]
    pushed = await _latest_requests(world)
    # api·web 만 PUSH 로 다시. web 은 api 를 기다린다. DB·worker 는 그대로다.
    assert pushed["api"].trigger_type == DeploymentTrigger.PUSH
    assert pushed["web"].trigger_type == DeploymentTrigger.PUSH
    assert pushed["worker"].id == before["worker"].id
    assert pushed["postgres"].id == before["postgres"].id
    assert await _jobs(world, pushed["api"].id) == [JobKind.BUILD]
    assert await _jobs(world, pushed["web"].id) == []
    assert sorted(receipt["deploymentRequestIds"]) == [pushed["api"].id, pushed["web"].id]
    assert len(receipt["repositoryAnalysisIds"]) == 1
    # 같은 delivery 재전송은 아무것도 늘리지 않는다.
    again = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert again.json()["data"]["repositoryAnalysisIds"] == []
    assert (await _latest_requests(world))["api"].id == pushed["api"].id
    async with world.factory() as session:
        analyses = (
            await session.scalars(
                select(RepositoryAnalysis).where(
                    RepositoryAnalysis.stack_id == stack_id, RepositoryAnalysis.source_sha == SHA2
                )
            )
        ).all()
    assert [(a.source_sha, a.mode, a.status) for a in analyses] == [
        (SHA2, "force", RepositoryAnalysisStatus.QUEUED)
    ]

    # 재분석(새 unit admin) → 스택 pendingChanges.
    await _run_analyses(world, SHOP_FILES_V2)
    stack = await _stack(world, client, stack_id or 0)
    pending = stack["pendingChanges"]
    assert pending["analysisId"] == analyses[0].id and pending["sourceSha"] == SHA2
    assert {"type": "UNIT_ADDED", "unitId": "admin"} in pending["changes"]

    # 증분 apply: 기존 unit 은 그대로(중복 없음), admin 만 새로. pendingChanges 는 지워진다.
    await _finish_all(world)
    response = await _apply(world, client, analyses[0].id, ["api", "web", "worker", "admin"])
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    after = await _services(world)
    assert set(after) == {"api", "web", "worker", "admin", "postgres", "redis"}
    assert {u: s.id for u, s in after.items() if u in services} == {
        u: s.id for u, s in services.items()
    }
    assert after["admin"].stack_id == stack_id
    assert len(data["databases"]) == 2 and len(data["services"]) == 4
    async with world.factory() as session:
        keys = [
            (v.service_id, v.key) for v in (await session.scalars(select(ServiceVariable))).all()
        ]
    assert len(keys) == len(set(keys))
    stack = await _stack(world, client, stack_id or 0)
    assert "pendingChanges" not in stack and stack["analysisId"] == analyses[0].id
    # 다시 보내도(APPLIED) 같은 서비스다.
    replay = await _apply(world, client, analyses[0].id, ["api"])
    assert {s["id"] for s in replay.json()["data"]["services"]} == {
        s["id"] for s in data["services"]
    }
    assert applied["analysis_id"] != analyses[0].id


async def test_variable_validation_blocks_manual_deploy_with_issues_and_push_records_failure(
    world: World, client: AsyncClient
) -> None:
    await _applied_shop(world, client)
    await _finish_all(world)
    services = await _services(world)
    api, pg = services["api"], services["postgres"]
    # 참조 대신 localhost 값으로 바꾼다.
    response = await _call(
        world,
        client,
        "PUT",
        f"/api/v1/services/{api.id}/variables/DATABASE_URL",
        json={"value": "postgres://shop:pw@localhost:5432/shop"},
    )
    assert response.status_code == 200, response.text
    await _set_variable(world, client, api.id, "CACHE_HOST", "cache")

    validation = await _call(
        world, client, "GET", f"/api/v1/services/{api.id}/variables/validation"
    )
    data = validation.json()["data"]
    assert data["ok"] is False
    issues = {i["key"]: i for i in data["issues"]}
    assert issues["DATABASE_URL"]["code"] == "LOCALHOST_ADDRESS"
    assert issues["DATABASE_URL"]["suggestion"] == {
        "reference": {"serviceId": pg.id, "property": "url"}
    }
    assert (issues["CACHE_HOST"]["code"], issues["CACHE_HOST"]["severity"]) == (
        "UNRESOLVABLE_HOST",
        "error",
    )
    assert "pw" not in validation.text

    blocked = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{api.id}/deployments",
        json={"triggerType": "MANUAL"},
    )
    assert blocked.status_code == 422
    body = blocked.json()
    assert body["code"] == "VARIABLES_INVALID"
    assert {d["field"]: d["reason"] for d in body["details"]} == {
        "CACHE_HOST": "UNRESOLVABLE_HOST",
        "DATABASE_URL": "LOCALHOST_ADDRESS",
    }
    assert body["data"]["serviceId"] == api.id and body["data"]["ok"] is False
    assert "localhost:5432" not in blocked.text
    forced = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{api.id}/deployments",
        json={"triggerType": "MANUAL", "skipVariableValidation": True},
    )
    assert forced.status_code == 201, forced.text
    await _succeed(world, (await _latest_requests(world))["api"])

    # 푸시 자동 배포는 422 대신 실패한 요청(VARIABLES_INVALID)으로 남기고, web 은 보류된다.
    raw, headers = _push("d-2", ["api/index.js", "web/x.conf"])
    receipt = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert receipt.status_code == 200
    pushed = await _latest_requests(world)
    assert (pushed["api"].status, pushed["api"].failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.VARIABLES_INVALID,
    )
    assert (pushed["web"].status, pushed["web"].failure_code) == (
        DeploymentStatus.FAILED,
        FailureCode.DEPENDENCY_FAILED,
    )
    assert pushed["api"].id not in receipt.json()["data"]["deploymentRequestIds"]
    assert await _jobs(world, pushed["api"].id) == []

    # 참조 대상이 지워지면 REFERENCE_BROKEN.
    async with world.factory.begin() as session:
        await session.execute(
            update(Service).where(Service.id == services["redis"].id).values(is_deleted=True)
        )
    validation = await _call(
        world, client, "GET", f"/api/v1/services/{api.id}/variables/validation"
    )
    codes = {i["key"]: i["code"] for i in validation.json()["data"]["issues"]}
    assert codes["REDIS_URL"] == "REFERENCE_BROKEN"


async def test_existing_service_without_analysis_skips_required_check(
    world: World, client: AsyncClient
) -> None:
    async with world.factory.begin() as session:
        installation = (await session.scalars(select(GithubInstallation))).one()
        service = await add(
            session,
            Service(
                project_id=world.project_id,
                name="legacy",
                source_repository_url=REPOSITORY_URL,
                github_installation_id=installation.id,
                source_branch="main",
            ),
        )
    response = await _call(
        world, client, "GET", f"/api/v1/services/{service.id}/variables/validation"
    )
    assert response.json()["data"] == {"ok": True, "issues": []}
    deploy = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{service.id}/deployments",
        json={"triggerType": "MANUAL"},
    )
    assert deploy.status_code == 201, deploy.text
    detail = (await _call(world, client, "GET", f"/api/v1/services/{service.id}")).json()["data"]
    assert detail["kind"] == "APP" and "stack" not in detail and "hostAliases" not in detail


async def test_database_api_creates_and_deploys_and_rejects_onprem_and_disabled(
    world: World, client: AsyncClient
) -> None:
    url = f"/api/v1/projects/{world.project_id}/databases"
    created = await _call(world, client, "POST", url, json={"name": "cache", "engine": "redis"})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert (data["kind"], data["databaseEngine"], data["internalPort"]) == (
        "DATABASE",
        "redis",
        6379,
    )
    assert data["connection"]["urlTemplate"] == (
        f"redis://default:****@app.svc-{data['id']}.svc.cluster.local:6379"
    )
    assert data["latestDeployment"]["status"] == "DEPLOYING"
    assert await _jobs(world, data["latestDeployment"]["id"]) == [JobKind.DEPLOY]
    duplicate = await _call(world, client, "POST", url, json={"name": "cache", "engine": "redis"})
    assert duplicate.status_code == 409

    onprem = await _call(
        world, client, "POST", url, json={"name": "pg", "engine": "postgres", "targetIds": [2]}
    )
    assert onprem.status_code == 422
    assert onprem.json()["details"] == [
        {"field": "engine", "reason": "networking_unsupported_target"}
    ]
    too_big = await _call(
        world, client, "POST", url, json={"name": "pg", "engine": "postgres", "storageGi": 50}
    )
    assert too_big.status_code == 422
    # DB 는 단일 인스턴스이고 소스 설정이 없다.
    patch = await _call(
        world, client, "PATCH", f"/api/v1/services/{data['id']}", json={"port": 1234}
    )
    assert patch.status_code == 422

    world.settings = world.settings.model_copy(update={"project_networking_enabled": False})
    disabled = await _call(world, client, "POST", url, json={"name": "pg", "engine": "postgres"})
    assert disabled.status_code == 422
    assert disabled.json()["details"][0]["reason"] == "project_networking_disabled"
    for password in await _passwords(world):
        assert all(password not in text for text in world.texts)


async def test_host_aliases_patch_validates_and_rejects_onprem(
    world: World, client: AsyncClient
) -> None:
    await _applied_shop(world, client)
    services = await _services(world)
    api, web = services["api"], services["web"]
    ok = await _call(
        world,
        client,
        "PATCH",
        f"/api/v1/services/{web.id}",
        json={"hostAliases": [{"name": "backend", "targetServiceId": api.id, "port": 3000}]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["hostAliases"] == [
        {"name": "backend", "targetServiceId": api.id, "port": 3000}
    ]
    for bad in (
        [{"name": "app", "targetServiceId": api.id}],
        [{"name": "Bad_Name", "targetServiceId": api.id}],
        [{"name": "self", "targetServiceId": web.id}],
        [{"name": "x", "targetServiceId": 999999}],
    ):
        response = await _call(
            world, client, "PATCH", f"/api/v1/services/{web.id}", json={"hostAliases": bad}
        )
        assert response.status_code == 422, bad
    async with world.factory.begin() as session:
        await session.execute(update(Service).where(Service.id == web.id).values(is_deleted=False))
        from app.models import ServiceTarget

        await session.execute(
            update(ServiceTarget).where(ServiceTarget.service_id == web.id).values(target_id=2)
        )
    onprem = await _call(
        world,
        client,
        "PATCH",
        f"/api/v1/services/{web.id}",
        json={"hostAliases": [{"name": "backend", "targetServiceId": api.id}]},
    )
    assert onprem.status_code == 422
    assert onprem.json()["details"][0]["reason"] == "networking_unsupported_target"
    reference = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{web.id}/variables",
        json={"key": "API_URL", "reference": {"serviceId": api.id, "property": "url"}},
    )
    assert reference.status_code == 422


async def test_apply_on_onprem_target_skips_databases_and_networking(
    world: World, client: AsyncClient
) -> None:
    analysis_id = await _analyze(world, client, SHOP_FILES)
    response = await _apply(
        world, client, analysis_id, ["api", "web", "worker"], targetIds=[2], deploy=False
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["databases"] == []
    services = await _services(world)
    assert all(s.host_aliases is None for s in services.values())
    async with world.factory() as session:
        assert (await session.scalars(select(ServiceVariable))).all() == []
        stacks = (await session.scalars(select(ServiceStack))).all()
    assert len(stacks) == 1

    explicit = await _analyze(world, client, SHOP_FILES)
    rejected = await _apply(
        world,
        client,
        explicit,
        ["api"],
        targetIds=[2],
        dependencies=[{"dependencyId": "postgres", "provision": True}],
    )
    assert rejected.status_code == 422
    assert rejected.json()["details"][0]["reason"] == "networking_unsupported_target"


async def test_registered_server_target_rejects_networking_but_keeps_stack(
    world: World, client: AsyncClient
) -> None:
    # 사용자가 등록한 서버 타깃도 kind 가 ONPREM 이라 공용 onprem 과 같은 규칙이다: DB·별칭·참조
    # 변수는 거절하고(chart 0.9.0 키를 받지 않는다) 스택 묶음·순서는 그대로 쓴다.
    async with world.factory.begin() as session:
        target = await add(
            session,
            Target(
                name="onprem-k3x9q2ma",
                kind=TargetKind.ONPREM,
                domain_suffix="internal.likelion.uk",
                owner_id=world.user.id,
            ),
        )
        await add(
            session,
            OnpremServer(
                owner_id=world.user.id,
                name="home-lab",
                server_key="k3x9q2ma",
                target_id=target.id,
                status=OnpremServerStatus.CONNECTED,
                registration_token_hash="0" * 64,
                registration_expires_at=now_utc(),
            ),
        )
    url = f"/api/v1/projects/{world.project_id}/databases"
    database = await _call(
        world,
        client,
        "POST",
        url,
        json={"name": "pg", "engine": "postgres", "targetIds": [target.id]},
    )
    assert database.status_code == 422
    assert database.json()["details"] == [
        {"field": "engine", "reason": "networking_unsupported_target"}
    ]

    analysis_id = await _analyze(world, client, SHOP_FILES)
    applied = await _apply(
        world, client, analysis_id, ["api", "web", "worker"], targetIds=[target.id], deploy=False
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["data"]["databases"] == []
    services = await _services(world)
    assert {s.stack_id for s in services.values()} != {None}
    assert all(s.host_aliases is None for s in services.values())
    async with world.factory() as session:
        assert (await session.scalars(select(ServiceVariable))).all() == []

    web, api = services["web"], services["api"]
    alias = await _call(
        world,
        client,
        "PATCH",
        f"/api/v1/services/{web.id}",
        json={"hostAliases": [{"name": "backend", "targetServiceId": api.id}]},
    )
    assert alias.status_code == 422
    assert alias.json()["details"][0]["reason"] == "networking_unsupported_target"
    reference = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{web.id}/variables",
        json={"key": "API_URL", "reference": {"serviceId": api.id, "property": "url"}},
    )
    assert reference.status_code == 422

    # 서버가 연결되지 않았으면 스택 push 는 그 서비스만 건너뛰고(409 로 웹훅을 실패시키지 않는다)
    # 재분석은 그대로 접수한다. 연결되면 같은 스택 순서로 다시 배포한다.
    async with world.factory.begin() as session:
        await session.execute(update(OnpremServer).values(status=OnpremServerStatus.PENDING))
    raw, headers = _push("d-server-1", ["web/nginx/default.conf"])
    skipped = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["data"]["deploymentRequestIds"] == []
    assert len(skipped.json()["data"]["repositoryAnalysisIds"]) == 1
    assert "web" not in await _latest_requests(world)

    async with world.factory.begin() as session:
        await session.execute(update(OnpremServer).values(status=OnpremServerStatus.CONNECTED))
    raw, headers = _push("d-server-2", ["web/nginx/default.conf"], sha="c" * 40)
    pushed = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert pushed.status_code == 200, pushed.text
    web_request = (await _latest_requests(world))["web"]
    assert pushed.json()["data"]["deploymentRequestIds"] == [web_request.id]
    assert web_request.trigger_type == DeploymentTrigger.PUSH


async def test_apply_without_provisioning_keeps_dependencies_unbound(
    world: World, client: AsyncClient
) -> None:
    analysis_id = await _analyze(world, client, SHOP_FILES)
    response = await _apply(
        world,
        client,
        analysis_id,
        ["api", "worker"],
        deploy=False,
        dependencies=[
            {"dependencyId": "postgres", "provision": False},
            {"dependencyId": "redis", "provision": True, "name": "cache", "storageGi": 2},
        ],
    )
    assert response.status_code == 201, response.text
    services = await _services(world)
    assert "postgres" not in services
    assert services["redis"].name == "cache"
    assert services["redis"].database_config is not None
    assert services["redis"].database_config["storageGi"] == 2
    unknown = await _analyze(world, client, SHOP_FILES)
    bad = await _apply(world, client, unknown, ["api"], dependencies=[{"dependencyId": "nope"}])
    assert bad.status_code == 422


async def test_stack_deployment_steps_status_is_recorded(world: World, client: AsyncClient) -> None:
    await _applied_shop(world, client)
    async with world.factory() as session:
        statuses = sorted(
            s.status for s in (await session.scalars(select(StackDeploymentStep))).all()
        )
    assert statuses == sorted(
        [StackDeploymentStepStatus.STARTED] * 2 + [StackDeploymentStepStatus.WAITING] * 3
    )
    listed = await _call(world, client, "GET", f"/api/v1/projects/{world.project_id}/stacks")
    assert len(listed.json()["data"]) == 1
    missing = await _call(world, client, "GET", f"/api/v1/projects/{world.project_id}/stacks/999")
    assert missing.status_code == 404 and missing.json()["code"] == "STACK_NOT_FOUND"


async def test_deploy_worker_commits_database_and_stack_app_values_with_resolved_references(
    world: World, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """DB 는 빌드 없이 DEPLOY job 으로 values(workload database)를 커밋하고, 앱은 참조 변수를 DB 의
    현재 자격 증명으로 풀어 봉인한다. 두 values 모두 chart 0.9.0 schema 를 통과한다."""
    from app.clients.secret_sealer import SecretSealer
    from app.core.config import DeployWorkerSettings
    from app.services.deploy_service import DeployService
    from tests.sealed_support import make_controller_key, unseal
    from tests.test_deploy_flow import FakeArgo, FakeEcr, FakeGitOps
    from tests.test_stack_values import _validate

    caplog.set_level(logging.DEBUG)
    await _applied_shop(world, client)
    services = await _services(world)
    pg, api, redis = services["postgres"], services["api"], services["redis"]
    key, certificate = make_controller_key()
    gitops = FakeGitOps()
    deployer = DeployService(
        world.factory,
        gitops,  # type: ignore[arg-type]
        FakeArgo(),  # type: ignore[arg-type]
        FakeEcr(),  # type: ignore[arg-type]
        DeployWorkerSettings(
            aws_region="ap-northeast-2",
            gitops_repository="org/gitops",
            gitops_app_id=1,
            gitops_app_private_key="unused",
            gitops_installation_id=2,
            argocd_server_url="http://argocd.test",
            argocd_token="unused",
            project_networking_enabled=True,
        ),
        "worker-1",
        cipher=world.cipher,
        sealer=SecretSealer(certificate),
    )

    async def run_deploy(request_id: int) -> dict[str, Any]:
        async with world.factory() as session:
            job = (
                await session.scalars(
                    select(Job).where(
                        Job.deployment_request_id == request_id, Job.kind == JobKind.DEPLOY
                    )
                )
            ).one()
        await deployer.run(job)
        values: dict[str, Any] = json.loads(list(gitops.trees.values())[-1]["values.yaml"])
        return values

    requests = await _latest_requests(world)
    db_values = await run_deploy(requests["postgres"].id)
    _validate(json.dumps(db_values))
    assert db_values["workload"] == {"kind": "database"}
    assert db_values["projectId"] == str(world.project_id)
    assert db_values["database"]["engine"] == "postgres"
    assert db_values["database"]["image"].startswith("docker.io/library/postgres:16-alpine@sha256:")
    name = db_values["variables"]["name"]
    sealed = db_values["variables"]["encryptedData"]
    assert set(sealed) == {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
    password = unseal(key, sealed["POSTGRES_PASSWORD"], f"svc-{pg.id}", name)
    assert unseal(key, sealed["POSTGRES_USER"], f"svc-{pg.id}", name) == "shop"
    redis_values = await run_deploy(requests["redis"].id)
    assert "REDIS_PASSWORD" in redis_values["variables"]["encryptedData"]

    # DB 성공 → api 빌드가 끝났다고 치고 DEPLOY 로 넘긴다(BuildService._close 와 같은 전이).
    await _succeed(world, requests["postgres"])
    await _succeed(world, requests["redis"])
    async with world.factory.begin() as session:
        build = (
            await session.scalars(
                select(Build).where(Build.deployment_request_id == requests["api"].id)
            )
        ).one()
        build.start_snapshot(SHA)
        build.start_codebuild(
            "cb-1",
            Builder.DOCKERFILE,
            "123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/1",
            "b-1",
            {},
        )
        build.succeed("sha256:" + "1" * 64)
        status = DeploymentStatusService.create(session)
        await status.transition_status(requests["api"].id, DeploymentStatus.BUILDING)
        await status.transition_status(requests["api"].id, DeploymentStatus.DEPLOYING)
        session.add(
            Job(
                deployment_request_id=requests["api"].id,
                kind=JobKind.DEPLOY,
                payload={"build_id": build.id},
            )
        )
    api_values = await run_deploy(requests["api"].id)
    _validate(json.dumps(api_values))
    assert api_values["containerPort"] == 3000
    assert api_values["service"] == {"exposeContainerPort": True}
    assert api_values["projectId"] == str(world.project_id)
    assert sorted(api_values["hostAliases"], key=lambda a: a["name"]) == [
        {"name": "postgres", "target": f"app.svc-{pg.id}.svc.cluster.local"},
        {"name": "redis", "target": f"app.svc-{redis.id}.svc.cluster.local"},
    ]
    api_sealed = api_values["variables"]["encryptedData"]
    api_name = api_values["variables"]["name"]
    assert unseal(key, api_sealed["DATABASE_URL"], f"svc-{api.id}", api_name) == (
        f"postgres://shop:{password}@app.svc-{pg.id}.svc.cluster.local:5432/shop"
    )
    assert unseal(key, api_sealed["REDIS_URL"], f"svc-{api.id}", api_name).startswith(
        "redis://default:"
    )
    assert unseal(key, api_sealed["API_TOKEN"], f"svc-{api.id}", api_name) == "jwt-value"
    # 평문 비밀은 Git(values)·응답·로그 어디에도 없다.
    assert password not in json.dumps(api_values) and password not in json.dumps(db_values)
    assert password not in caplog.text
    assert all(password not in text for text in world.texts)


async def test_concurrent_predecessor_success_starts_dependent_exactly_once(
    world: World, client: AsyncClient
) -> None:
    """postgres·redis 가 서로 다른 트랜잭션에서 동시에 성공해도 api 는 정확히 한 번 시작한다."""
    import asyncio

    await _applied_shop(world, client)
    requests = await _latest_requests(world)
    await asyncio.gather(
        _move(world, requests["postgres"].id, DeploymentStatus.SUCCEEDED),
        _move(world, requests["redis"].id, DeploymentStatus.SUCCEEDED),
    )
    assert await _jobs(world, requests["api"].id) == [JobKind.BUILD]
    assert await _jobs(world, requests["worker"].id) == [JobKind.BUILD]
    async with world.factory() as session:
        step = (
            await session.scalars(
                select(StackDeploymentStep).where(
                    StackDeploymentStep.deployment_request_id == requests["api"].id
                )
            )
        ).one()
    assert step.status == StackDeploymentStepStatus.STARTED and step.started_at is not None
