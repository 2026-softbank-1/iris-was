# ruff: noqa: F811, E501
"""원클릭 흐름: 비밀값 생성·공유와 URL 사용자 보존(계약 G). TEST_DATABASE_URL 이 필요하다.

Temp_log(app + 커스텀 mongo, 초기화 스크립트가 `MONGO_APP_PASSWORD` 로 archlog 사용자를 만든다)를
실제 분석기 subprocess 로 분석한다(GitHub 만 가짜). 사용자 입력 없이 apply → 검증 ok →
DB·앱이 같은 MONGO_APP_PASSWORD → MONGO_URI 는 archlog 사용자 + 그 비밀값 → DB values 에 봉인.
"""

import json
import logging
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.clients.secret_sealer import SecretSealer
from app.clients.source_repository_client import CommitInfo
from app.core.config import DeployWorkerSettings
from app.enums import JobKind
from app.models import Job, ServiceVariable
from app.services.deploy_service import DeployService, service_namespace
from tests.sealed_support import make_controller_key, unseal
from tests.test_deploy_flow import FakeArgo, FakeEcr, FakeGitOps
from tests.test_stack_flow import (  # noqa: F401
    REPOSITORY,
    SHA2,
    World,
    _analyze,
    _apply,
    _call,
    _latest_requests,
    _services,
    client,
    session_factory,
    world,
)
from tests.worker_support import requires_database

pytestmark = [pytest.mark.integration, requires_database]

TEMP_LOG_FILES: dict[str, bytes] = {
    "compose.yaml": b"""services:
  app:
    build: .
    ports: ['127.0.0.1:${APP_PORT:-8080}:4000']
    environment:
      NODE_ENV: production
      SESSION_SECRET: ${SESSION_SECRET:?Run make init}
      MONGO_URI: mongodb://archlog:${MONGO_APP_PASSWORD:?Run make init}@mongo:27017/archlog?authSource=archlog
    depends_on:
      mongo: {condition: service_healthy}
  mongo:
    image: temp-log-mongo:local
    build: {context: ., dockerfile: Dockerfile.mongo}
    command: [mongod, --bind_ip_all, --auth]
    environment:
      MONGO_INITDB_ROOT_USERNAME: root
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_ROOT_PASSWORD:?Run make init}
      MONGO_APP_PASSWORD: ${MONGO_APP_PASSWORD:?Run make init}
      HOME: /tmp
    volumes:
      - mongo-data:/data/db
      - ./docker/mongo-init.js:/docker-entrypoint-initdb.d/init.js:ro
volumes:
  mongo-data:
""",
    "Dockerfile": b'FROM node:22-alpine\nCOPY . .\nEXPOSE 4000\nCMD ["node","server/index.js"]\n',
    "package.json": b'{"name":"temp-log"}\n',
    "Dockerfile.mongo": b"FROM mongo:8.0.32\n",
    ".env.example": (
        b"APP_PORT=8080\nSESSION_SECRET=generate-a-random-secret-with-make-init\n"
        b"MONGO_ROOT_PASSWORD=generate-with-make-init\nMONGO_APP_PASSWORD=generate-with-make-init\n"
    ),
    "docker/mongo-init.js": (
        b"db.getSiblingDB('archlog').createUser({user: 'archlog',"
        b" pwd: process.env.MONGO_APP_PASSWORD, roles: ['readWrite']});\n"
    ),
    "server/index.js": (
        b"const s = process.env.SESSION_SECRET;\nconst uri = process.env.MONGO_URI;\n"
        b"app.listen(4000);\n"
    ),
}


def _deployer(world: World) -> tuple[DeployService, Any]:
    key, certificate = make_controller_key()
    deployer = DeployService(
        world.factory,
        FakeGitOps(),  # type: ignore[arg-type]
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
    return deployer, key


async def _variables(world: World, service_id: int) -> dict[str, ServiceVariable]:
    async with world.factory() as session:
        rows = await session.scalars(
            select(ServiceVariable).where(ServiceVariable.service_id == service_id)
        )
        return {v.key: v for v in rows.all()}


async def test_temp_log_one_click_apply_generates_shared_secrets_and_app_user_url(
    world: World, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    analysis_id = await _analyze(world, client, TEMP_LOG_FILES)

    # 분석 조회: 플랫폼이 띄울 이미지는 compose 의 mongo:8 이 아니라 고정 mongo:7 이다.
    got = await _call(
        world,
        client,
        "GET",
        f"/api/v1/projects/{world.project_id}/repository-analyses/{analysis_id}",
    )
    data = got.json()["data"]
    assert {s["id"] for s in data["result"]["secrets"]} == {
        "MONGO_APP_PASSWORD",
        "MONGO_ROOT_PASSWORD",
        "SESSION_SECRET",
    }
    provisioning = data["provisioning"]["mongo"]
    assert provisioning["engine"] == "mongodb"
    assert provisioning["image"].startswith("docker.io/library/mongo:7@sha256:")

    # 사용자 입력 없이 apply 한 번으로 배포까지 접수된다.
    applied = await _apply(world, client, analysis_id, ["app"])
    assert applied.status_code == 201, applied.text
    body = applied.json()["data"]
    assert "variableIssues" not in body and body["stackDeploymentId"] > 0
    services = await _services(world)
    app, mongo = services["app"], services["mongo"]
    assert {g["id"]: sorted(g["serviceIds"]) for g in body["generatedSecrets"]} == {
        "MONGO_APP_PASSWORD": sorted([app.id, mongo.id]),
        "SESSION_SECRET": [app.id],
    }

    validation = await _call(
        world, client, "GET", f"/api/v1/services/{app.id}/variables/validation"
    )
    assert validation.json()["data"] == {"ok": True, "issues": []}

    app_vars, db_vars = await _variables(world, app.id), await _variables(world, mongo.id)
    shared = world.cipher.decrypt(app_vars["MONGO_APP_PASSWORD"].encrypted_value or "")
    assert world.cipher.decrypt(db_vars["MONGO_APP_PASSWORD"].encrypted_value or "") == shared
    assert len(shared) >= 40
    session_secret = world.cipher.decrypt(app_vars["SESSION_SECRET"].encrypted_value or "")
    # 앱은 쓰지 않는 MONGO_ROOT_PASSWORD 를 받지 않는다(DB 루트 비밀번호는 플랫폼 관리 값).
    assert "MONGO_ROOT_PASSWORD" not in app_vars
    root_password = world.cipher.decrypt(
        db_vars["MONGO_INITDB_ROOT_PASSWORD"].encrypted_value or ""
    )
    assert app_vars["MONGO_URI"].reference == {
        "serviceId": mongo.id,
        "property": "url",
        "scheme": "mongodb",
        "suffix": "/archlog?authSource=archlog",
        "user": "archlog",
        "passwordVariable": "MONGO_APP_PASSWORD",
    }

    # 변수 API 미리보기: archlog 사용자, 비밀번호는 가린다.
    listed = await _call(world, client, "GET", f"/api/v1/services/{app.id}/variables")
    uri = next(v for v in listed.json()["data"]["variables"] if v["key"] == "MONGO_URI")
    host = f"app.svc-{mongo.id}.svc.cluster.local"
    assert uri["resolved"] == f"mongodb://archlog:****@{host}:27017/archlog?authSource=archlog"

    # Deploy Worker: DB 를 먼저 배포한다. values 에 MONGO_APP_PASSWORD 가 봉인돼 있다.
    deployer, key = _deployer(world)
    requests = await _latest_requests(world)
    async with world.factory() as session:
        job = (
            await session.scalars(
                select(Job).where(
                    Job.deployment_request_id == requests["mongo"].id, Job.kind == JobKind.DEPLOY
                )
            )
        ).one()
    await deployer.run(job)
    gitops: FakeGitOps = deployer._github  # type: ignore[assignment]
    values = json.loads(list(gitops.trees.values())[-1]["values.yaml"])
    sealed = values["variables"]
    assert "MONGO_APP_PASSWORD" in sealed["encryptedData"]
    assert (
        unseal(
            key,
            sealed["encryptedData"]["MONGO_APP_PASSWORD"],
            service_namespace(mongo.id),
            sealed["name"],
        )
        == shared
    )

    # 앱 MONGO_URI 는 같은 비밀값으로 풀린다(배포 직전 해석).
    from app.repositories.service_repository import ServiceRepository
    from app.repositories.service_variable_repository import ServiceVariableRepository
    from app.services.variable_references import ReferenceResolver, VariableReference

    async with world.factory() as session:
        resolver = ReferenceResolver(
            ServiceRepository(session),
            ServiceVariableRepository(session),
            is_networking_enabled=True,
            cipher=world.cipher,
        )
        resolved = await resolver.resolve(
            app, VariableReference.from_json(app_vars["MONGO_URI"].reference or {}), masked=False
        )
    assert resolved.value == (f"mongodb://archlog:{shared}@{host}:27017/archlog?authSource=archlog")

    # 비밀값은 apply·분석·검증 응답과 로그에 없다(변수 목록은 소유자에게 값 변수를 보여 주는 기존
    # 정책이라 뺀다).
    texts = [t for t in world.texts if t != listed.text]
    logs = "\n".join(r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records)
    for secret in (shared, session_secret, root_password):
        assert all(secret not in text for text in texts)
        assert secret not in logs


async def test_temp_log_incremental_apply_keeps_shared_secret(
    world: World, client: AsyncClient
) -> None:
    first_id = await _analyze(world, client, TEMP_LOG_FILES)
    assert (await _apply(world, client, first_id, ["app"])).status_code == 201
    services = await _services(world)
    before = await _variables(world, services["app"].id)

    # 같은 레포를 다시 분석해 증분 apply: 값은 그대로, 새로 만든 비밀값 없음.
    world.github_api.heads[(REPOSITORY, "main")] = CommitInfo(SHA2, "second")
    second_id = await _analyze(world, client, TEMP_LOG_FILES)
    again = await _apply(world, client, second_id, ["app"])
    assert again.status_code == 201, again.text
    assert "generatedSecrets" not in again.json()["data"]
    after = await _variables(world, services["app"].id)
    for key in ("MONGO_APP_PASSWORD", "SESSION_SECRET"):
        assert after[key].encrypted_value == before[key].encrypted_value
