"""compose `build.target` 과 URL 경로·쿼리 보존 통합 테스트(리뷰 P1 두 건). TEST_DATABASE_URL 필요.

실제 분석기 subprocess·Control API·DB 를 쓰고 GitHub·CodeBuild 만 가짜다.
- `target: api` → 서비스 docker_target → CodeBuild 환경변수 DOCKER_TARGET.
- `API_URL=http://api:3000/api/v1?tenant=demo` → 내부 주소 + 같은 경로·쿼리.
- DB URL 의 `?sslmode=disable` 은 플랫폼이 만든 자격 증명과 함께 남는다.
"""

# ruff: noqa: F811  (test_stack_flow 의 fixture 를 가져와 인자로 받는다)
import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Job, ServiceVariable
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.build_service import BuildService
from app.services.variable_references import ReferenceResolver, VariableReference
from tests.test_build_service import (
    SETTINGS,
    SUCCEEDED,
    FakeArtifacts,
    FakeBuildLogs,
    FakeCodeBuild,
    FakeEcr,
)
from tests.test_stack_flow import (  # noqa: F401
    SHA,
    World,
    _analyze,
    _apply,
    _call,
    _jobs,
    _latest_requests,
    _push,
    _run_analyses,
    _services,
    _set_variable,
    _stack,
    _succeed,
    client,
    session_factory,
    world,
)
from tests.worker_support import requires_database

pytestmark = [pytest.mark.integration, requires_database]

COMPOSE = b"""services:
  api:
    build:
      context: ./api
      target: api
    ports: ["3000:3000"]
    environment:
      DATABASE_URL: postgres://shop:secret@postgres:5432/shop?sslmode=disable
      REDIS_URL: redis://redis:6379/2
  web:
    build: ./web
    ports: ["8080:80"]
    environment:
      API_URL: http://api:3000/api/v1?tenant=demo
    depends_on: [api]
  postgres:
    image: postgres:16-alpine
  redis:
    image: redis:7-alpine
"""
FILES: dict[str, bytes] = {
    "compose.yaml": COMPOSE,
    "api/Dockerfile": (
        b"FROM node:20-alpine AS base\nWORKDIR /app\nCOPY . .\n"
        b'FROM base AS api\nEXPOSE 3000\nCMD ["node","index.js"]\n'
        b'FROM base AS worker\nCMD ["node","worker.js"]\n'
    ),
    "api/package.json": b'{"name":"api"}\n',
    "web/Dockerfile": b"FROM nginx:alpine\nEXPOSE 80\n",
}
# 사용자가 target 을 바꾼 다음 커밋.
FILES_V2 = {**FILES, "compose.yaml": COMPOSE.replace(b"target: api", b"target: worker")}


async def _resolve(world: World, owner_key: str, key: str, *, masked: bool) -> str:
    services = await _services(world)
    async with world.factory() as session:
        variable = (
            await session.scalars(
                select(ServiceVariable).where(
                    ServiceVariable.service_id == services[owner_key].id, ServiceVariable.key == key
                )
            )
        ).one()
        assert variable.reference is not None
        resolver = ReferenceResolver(
            ServiceRepository(session),
            ServiceVariableRepository(session),
            is_networking_enabled=True,
            cipher=world.cipher,
        )
        resolved = await resolver.resolve(
            services[owner_key], VariableReference.from_json(variable.reference), masked=masked
        )
    return resolved.value


async def _apply_all(world: World, client: AsyncClient) -> int:
    analysis_id = await _analyze(world, client, FILES)
    response = await _apply(world, client, analysis_id, ["api", "web"])
    assert response.status_code == 201, response.text
    return analysis_id


async def test_compose_target_reaches_service_and_codebuild_override(
    world: World, client: AsyncClient
) -> None:
    await _apply_all(world, client)
    services = await _services(world)
    assert services["api"].docker_target == "api"
    assert services["web"].docker_target is None
    response = await _call(world, client, "GET", f"/api/v1/services/{services['api'].id}")
    assert response.json()["data"]["dockerTarget"] == "api"

    # DB 가 끝나야 api 빌드가 시작된다.
    for _ in range(2):
        for key in ("postgres", "redis"):
            request = (await _latest_requests(world))[key]
            if await _jobs(world, request.id) and request.status.value == "QUEUED":
                await _succeed(world, request)
    # DB 가 끝나야 api 빌드가 시작된다.
    for key in ("postgres", "redis"):
        await _succeed(world, (await _latest_requests(world))[key])
    requests = await _latest_requests(world)
    codebuild = FakeCodeBuild(SUCCEEDED)
    builder = BuildService(
        world.factory,
        _ArchiveGitHub(),  # type: ignore[arg-type]
        codebuild,  # type: ignore[arg-type]
        FakeEcr(),  # type: ignore[arg-type]
        FakeArtifacts(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        FakeBuildLogs(),  # type: ignore[arg-type]
        SETTINGS,
        "worker-1",
    )
    async with world.factory() as session:
        job = (
            await session.scalars(
                select(Job).where(Job.deployment_request_id == requests["api"].id)
            )
        ).one()
    assert await _jobs(world, requests["api"].id)
    claimed = await builder.claim_next_job()
    assert claimed is not None and claimed.id == job.id
    await builder.run(claimed, asyncio.Event())
    env = codebuild.started_envs[0]
    assert (env["BUILDER"], env["DOCKER_TARGET"]) == ("dockerfile", "api")


async def test_patch_docker_target_validates_and_clears(world: World, client: AsyncClient) -> None:
    await _apply_all(world, client)
    service_id = (await _services(world))["api"].id
    url = f"/api/v1/services/{service_id}"
    bad = await _call(world, client, "PATCH", url, json={"dockerTarget": "bad target;rm"})
    assert bad.status_code == 422
    ok = await _call(world, client, "PATCH", url, json={"dockerTarget": "release.v1"})
    assert ok.status_code == 200 and ok.json()["data"]["dockerTarget"] == "release.v1"
    cleared = await _call(world, client, "PATCH", url, json={"dockerTarget": None})
    assert "dockerTarget" not in cleared.json()["data"]


async def test_url_suffix_is_stored_resolved_and_masked(world: World, client: AsyncClient) -> None:
    await _apply_all(world, client)
    services = await _services(world)
    web, api, pg, redis = (services[k] for k in ("web", "api", "postgres", "redis"))
    async with world.factory() as session:
        rows = {
            (v.service_id, v.key): v.reference
            for v in (await session.scalars(select(ServiceVariable))).all()
        }
    assert rows[(web.id, "API_URL")] == {
        "serviceId": api.id,
        "property": "url",
        "scheme": "http",
        "suffix": "/api/v1?tenant=demo",
    }
    assert rows[(api.id, "DATABASE_URL")] == {
        "serviceId": pg.id,
        "property": "url",
        "scheme": "postgres",
        "suffix": "/shop?sslmode=disable",
    }

    host = f"app.svc-{api.id}.svc.cluster.local"
    assert await _resolve(world, "web", "API_URL", masked=False) == (
        f"http://{host}:3000/api/v1?tenant=demo"
    )
    pg_host = f"app.svc-{pg.id}.svc.cluster.local"
    database_url = await _resolve(world, "api", "DATABASE_URL", masked=False)
    # 코드가 쓴 `secret` 이 아니라 플랫폼이 만든 비밀번호다. 쿼리는 그대로다.
    assert database_url.startswith("postgres://shop:") and "secret" not in database_url
    assert database_url.endswith(f"@{pg_host}:5432/shop?sslmode=disable")
    assert await _resolve(world, "api", "REDIS_URL", masked=True) == (
        f"redis://default:****@app.svc-{redis.id}.svc.cluster.local:6379/2"
    )

    # 변수 API: 참조에 scheme·suffix 가 있고 미리보기는 비밀번호를 가린다.
    response = await _call(world, client, "GET", f"/api/v1/services/{api.id}/variables")
    by_key = {v["key"]: v for v in response.json()["data"]["variables"]}
    assert by_key["DATABASE_URL"]["reference"] == {
        "serviceId": pg.id,
        "property": "url",
        "scheme": "postgres",
        "suffix": "/shop?sslmode=disable",
    }
    assert by_key["DATABASE_URL"]["resolved"] == (
        f"postgres://shop:****@{pg_host}:5432/shop?sslmode=disable"
    )


async def test_variable_api_accepts_and_rejects_scheme_and_suffix(
    world: World, client: AsyncClient
) -> None:
    await _apply_all(world, client)
    services = await _services(world)
    web, api = services["web"], services["api"]
    url = f"/api/v1/services/{web.id}/variables"

    def body(**reference: Any) -> dict[str, Any]:
        return {
            "key": "OTHER_URL",
            "reference": {"serviceId": api.id, "property": "url", **reference},
        }

    created = await _call(
        world, client, "POST", url, json=body(scheme="ws", suffix="/socket?token=1#top")
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["resolved"] == (
        f"ws://app.svc-{api.id}.svc.cluster.local:3000/socket?token=1#top"
    )
    for bad in (
        {"suffix": "api"},
        {"suffix": "/a b"},
        {"suffix": "/" + "a" * 2048},
        {"scheme": "HTTP"},
        {"scheme": "1http"},
    ):
        response = await _call(world, client, "POST", url, json={**body(**bad), "key": "BAD_URL"})
        assert response.status_code == 422, (bad, response.text)
    not_url = await _call(
        world,
        client,
        "POST",
        url,
        json={
            "key": "BAD_HOST",
            "reference": {"serviceId": api.id, "property": "host", "suffix": "/x"},
        },
    )
    assert not_url.status_code == 422


async def test_stored_scheme_mismatch_warns_and_target_change_is_pending_then_incremental(
    world: World, client: AsyncClient
) -> None:
    analysis_id = await _apply_all(world, client)
    services = await _services(world)
    api, redis = services["api"], services["redis"]
    # 저장한 스킴이 mysql 인데 redis 를 가리킨다.
    response = await _call(
        world,
        client,
        "POST",
        f"/api/v1/services/{api.id}/variables",
        json={
            "key": "CACHE_URL",
            "reference": {"serviceId": redis.id, "property": "url", "scheme": "mysql"},
        },
    )
    assert response.status_code == 201, response.text
    validation = await _call(
        world, client, "GET", f"/api/v1/services/{api.id}/variables/validation"
    )
    issues = validation.json()["data"]["issues"]
    assert [(i["key"], i["code"], i["severity"]) for i in issues] == [
        ("CACHE_URL", "SCHEME_MISMATCH", "warning")
    ]

    # 재분석에서 target 이 worker 로 바뀌면 pendingChanges 에 알리고, 증분 apply 가 서비스를 고친다.
    stack_id = api.stack_id
    raw, headers = _push("d-1", ["api/Dockerfile", "compose.yaml"])
    pushed = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert pushed.status_code == 200, pushed.text
    await _run_analyses(world, FILES_V2)
    stack = await _stack(world, client, stack_id or 0)
    assert {
        "type": "UNIT_CHANGED",
        "unitId": "api",
        "field": "buildTarget",
        "from": "api",
        "to": "worker",
    } in stack["pendingChanges"]["changes"]
    assert stack["pendingChanges"]["analysisId"] != analysis_id
    response = await _apply(world, client, stack["pendingChanges"]["analysisId"], ["api", "web"])
    assert response.status_code == 201, response.text
    assert (await _services(world))["api"].docker_target == "worker"


class _ArchiveGitHub:
    """BuildService 가 받는 GitHub 의 가짜. FILES 를 tarball 로 준다."""

    async def create_installation_token(self, installation_id: int, name: str | None) -> str:
        return "token"

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        return SHA

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Any, max_bytes: int
    ) -> None:
        import io
        import tarfile

        with tarfile.open(dest, "w:gz") as archive:
            for name, content in FILES.items():
                info = tarfile.TarInfo(f"owner-shop-{sha[:7]}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
