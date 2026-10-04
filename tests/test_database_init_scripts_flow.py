# ruff: noqa: F811
"""DB 초기화 스크립트 통합 테스트. TEST_DATABASE_URL 의 로컬 PostgreSQL 이 필요하다.

iris-multi-image-shop 처럼 postgres 가 compose 로 `./db/schema.sql`·`./db/seed.sql` 을
`/docker-entrypoint-initdb.d` 에 mount 하는 레포를 실제 분석기 subprocess 로 분석한다(GitHub 만
가짜).
분석(내용 재확인·저장) → apply(DB 서비스로 복사, 응답은 메타데이터만) → Deploy Worker values
(`database.initScripts`, chart 0.9.0 schema 통과) → push 로 seed 가 바뀌면 pendingChanges ·
증분 apply 응답에 DEPENDENCY_CHANGED(init_scripts_changed), 이미 있는 DB 는 그대로.
"""

import hashlib
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.clients.secret_sealer import SecretSealer
from app.core.config import DeployWorkerSettings
from app.enums import JobKind
from app.models import DatabaseInitScript, Job, RepositoryAnalysis, Service
from app.services.deploy_service import DeployService
from tests.sealed_support import make_controller_key
from tests.test_deploy_flow import FakeArgo, FakeEcr, FakeGitOps

# 스택 흐름 테스트의 fixture(world·client·session_factory)를 그대로 쓴다. 인자 이름이 같아 F811.
from tests.test_stack_flow import (  # noqa: F401
    SHOP_FILES,
    World,
    _analyze,
    _apply,
    _call,
    _finish_all,
    _latest_requests,
    _push,
    _run_analyses,
    _services,
    _set_variable,
    _stack,
    client,
    session_factory,
    world,
)
from tests.test_stack_values import _FIXTURE_CHART, _validate
from tests.worker_support import requires_database

pytestmark = [pytest.mark.integration, requires_database]

SCHEMA_SQL = (
    b"BEGIN;\nCREATE TABLE jobs (\n  id BIGSERIAL PRIMARY KEY,\n  kind TEXT NOT NULL,\n"
    b"  status TEXT NOT NULL DEFAULT 'queued'\n);\nCOMMIT;\n"
)
SEED_SQL = b"INSERT INTO jobs (kind) VALUES ('welcome-email');\n"
SEED_SQL_V2 = SEED_SQL + b"INSERT INTO jobs (kind) VALUES ('digest');\n"
SHOP_INITDB_FILES: dict[str, bytes] = {
    **SHOP_FILES,
    "compose.yaml": SHOP_FILES["compose.yaml"].replace(
        b"      POSTGRES_PASSWORD: secret\n",
        b"      POSTGRES_PASSWORD: secret\n"
        b"    volumes:\n"
        b"      - postgres_data:/var/lib/postgresql/data\n"
        b"      - ./db/schema.sql:/docker-entrypoint-initdb.d/001-schema.sql:ro\n"
        b"      - ./db/seed.sql:/docker-entrypoint-initdb.d/002-seed.sql:ro\n",
    )
    + b"volumes:\n  postgres_data:\n",
    "db/schema.sql": SCHEMA_SQL,
    "db/seed.sql": SEED_SQL,
}


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _apply_shop(world: World, client: AsyncClient) -> int:
    analysis_id = await _analyze(world, client, SHOP_INITDB_FILES)
    first = await _apply(world, client, analysis_id, ["web", "api", "worker"])
    assert first.status_code == 201, first.text
    services = await _services(world)
    await _set_variable(world, client, services["api"].id, "JWT_SECRET", "jwt-value")
    second = await _apply(world, client, analysis_id, ["web"])
    assert second.status_code == 201, second.text
    return analysis_id


def _deployer(world: World) -> DeployService:
    _, certificate = make_controller_key()
    return DeployService(
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


async def _deploy_values(world: World, deployer: DeployService, request_id: int) -> dict[str, Any]:
    async with world.factory() as session:
        job = (
            await session.scalars(
                select(Job).where(
                    Job.deployment_request_id == request_id, Job.kind == JobKind.DEPLOY
                )
            )
        ).one()
    await deployer.run(job)
    gitops: FakeGitOps = deployer._github  # type: ignore[assignment]
    values: dict[str, Any] = json.loads(list(gitops.trees.values())[-1]["values.yaml"])
    return values


async def test_shop_init_scripts_flow_from_analysis_to_deploy_values(
    world: World, client: AsyncClient
) -> None:
    analysis_id = await _apply_shop(world, client)

    # 분석: 분석기는 경로·해시만 내고, Worker 가 소스에서 읽어 확인한 내용을 sha256 으로 저장했다.
    async with world.factory() as session:
        analysis = await session.get_one(RepositoryAnalysis, analysis_id)
        stored = {row.sha256: row for row in (await session.scalars(select(DatabaseInitScript)))}
    assert analysis.result is not None
    postgres = next(d for d in analysis.result["dependencies"] if d["id"] == "postgres")
    assert [(s["path"], s["order"], s["supported"]) for s in postgres["initScripts"]] == [
        ("db/schema.sql", 0, True),
        ("db/seed.sql", 1, True),
    ]
    assert set(stored) == {_sha(SCHEMA_SQL), _sha(SEED_SQL)}
    assert bytes(stored[_sha(SCHEMA_SQL)].content) == SCHEMA_SQL
    assert stored[_sha(SEED_SQL)].size_bytes == len(SEED_SQL)

    # apply: DB 서비스로 메타데이터만 복사했다. redis 는 스크립트가 없다.
    services = await _services(world)
    pg, redis = services["postgres"], services["redis"]
    assert pg.database_config is not None
    assert pg.database_config["initScripts"] == [
        {
            "name": "00-schema.sql",
            "path": "db/schema.sql",
            "kind": "sql",
            "sha256": _sha(SCHEMA_SQL),
            "size": len(SCHEMA_SQL),
        },
        {
            "name": "01-seed.sql",
            "path": "db/seed.sql",
            "kind": "sql",
            "sha256": _sha(SEED_SQL),
            "size": len(SEED_SQL),
        },
    ]
    assert "initScripts" not in (redis.database_config or {})

    # 서비스 응답: 이름·경로·해시·크기만. 내용은 어떤 응답에도 없다.
    detail = await _call(world, client, "GET", f"/api/v1/services/{pg.id}")
    assert detail.json()["data"]["database"]["initScripts"] == [
        {"name": "00-schema.sql", "path": "db/schema.sql", "sha256": _sha(SCHEMA_SQL),
         "size": len(SCHEMA_SQL)},
        {"name": "01-seed.sql", "path": "db/seed.sql", "sha256": _sha(SEED_SQL),
         "size": len(SEED_SQL)},
    ]  # fmt: skip
    assert all(b"CREATE TABLE jobs" not in text.encode() for text in world.texts)

    # Deploy Worker: values 에 schema·seed 내용이 이름순으로 들어가고 chart 0.9.0 schema 를
    # 통과한다.
    deployer = _deployer(world)
    requests = await _latest_requests(world)
    db_values = await _deploy_values(world, deployer, requests["postgres"].id)
    assert db_values["database"]["initScripts"] == [
        {"name": "00-schema.sql", "content": SCHEMA_SQL.decode()},
        {"name": "01-seed.sql", "content": SEED_SQL.decode()},
    ]
    _validate(json.dumps(db_values))
    _validate_with_fixture_chart(db_values)
    redis_values = await _deploy_values(world, deployer, requests["redis"].id)
    assert "initScripts" not in redis_values["database"]
    _validate(json.dumps(redis_values))


def _validate_with_fixture_chart(values: dict[str, Any]) -> None:
    """레포에 고정한 chart 사본(tests/fixtures)으로도 검사한다(작업 공간 infra 가 없어도 같다)."""
    import jsonschema
    import yaml

    from tests.test_stack_values import _merge

    schema = json.loads((_FIXTURE_CHART / "values.schema.json").read_text())
    defaults = yaml.safe_load((_FIXTURE_CHART / "values.yaml").read_text())
    jsonschema.validate(_merge(defaults, values), schema)


async def test_changed_init_scripts_are_reported_but_not_rerun_on_existing_database(
    world: World, client: AsyncClient
) -> None:
    await _apply_shop(world, client)
    await _finish_all(world)
    services = await _services(world)
    pg = services["postgres"]
    stack_id = pg.stack_id or 0
    before_config = dict(pg.database_config or {})
    before_requests = await _latest_requests(world)

    # seed 를 바꾼 push → 같은 커밋 재분석 → 스택 pendingChanges 에 DEPENDENCY_CHANGED.
    raw, headers = _push("initdb-1", ["db/seed.sql"])
    response = await _call(
        world, client, "POST", "/api/v1/webhooks/github", content=raw, headers=headers
    )
    assert response.status_code == 200, response.text
    await _run_analyses(world, {**SHOP_INITDB_FILES, "db/seed.sql": SEED_SQL_V2})
    stack = await _stack(world, client, stack_id)
    pending = stack["pendingChanges"]
    change = next(c for c in pending["changes"] if c["type"] == "DEPENDENCY_CHANGED")
    assert (change["unitId"], change["field"], change["reason"]) == (
        "postgres",
        "initScripts",
        "init_scripts_changed",
    )
    assert "not re-initialized" in change["message"]
    assert change["from"][1] == {"path": "db/seed.sql", "sha256": _sha(SEED_SQL)}
    assert change["to"][1] == {"path": "db/seed.sql", "sha256": _sha(SEED_SQL_V2)}

    # 증분 apply: 응답이 같은 변경(serviceId 포함)을 알리고, DB 설정·배포는 그대로다.
    applied = await _apply(world, client, pending["analysisId"], ["api", "web", "worker"])
    assert applied.status_code == 201, applied.text
    changes = applied.json()["data"]["changes"]
    assert [(c["type"], c["unitId"], c["reason"], c["serviceId"]) for c in changes] == [
        ("DEPENDENCY_CHANGED", "postgres", "init_scripts_changed", pg.id)
    ]
    async with world.factory() as session:
        after = await session.get_one(Service, pg.id)
    assert after.database_config == before_config
    after_requests = await _latest_requests(world)
    assert after_requests["postgres"].id == before_requests["postgres"].id
    stack = await _stack(world, client, stack_id)
    assert "pendingChanges" not in stack
