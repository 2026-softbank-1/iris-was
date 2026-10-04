"""Deploy Worker 가 쓰는 GitOps values 가 iris-service chart 0.8.0 schema 를 통과하는지 본다.

schema 는 `IRIS_SERVICE_CHART_DIR`, 같은 작업 공간의 iris-infra 체크아웃(`../infra`), 없으면
`tests/fixtures/iris-service-chart`(chart 0.8.0 의 values.schema.json·values.yaml 사본) 순서로
읽는다.
helm 처럼 chart 기본값(values.yaml)에 Worker 값을 덮어 합친 뒤 검사한다. 기능이 꺼진 Worker 의
values 는 이전과 같아야 한다(새 키가 없다).
"""

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from app.enums import Builder, DatabaseEngine
from app.services.builder_detection import DeployConfig
from app.services.database_engines import ENGINE_SPECS, resolve_image
from app.services.deploy_service import (
    NetworkingValues,
    render_database_values,
    render_service_values,
)
from app.services.scaling_config import ScalingConfig

_WORKSPACE_CHART = Path(__file__).resolve().parents[2] / "infra/helm/charts/iris-service"
_FIXTURE_CHART = Path(__file__).resolve().parent / "fixtures/iris-service-chart"
CHART_DIR = Path(
    os.environ.get("IRIS_SERVICE_CHART_DIR")
    or (_WORKSPACE_CHART if (_WORKSPACE_CHART / "values.schema.json").exists() else _FIXTURE_CHART)
)
requires_chart = pytest.mark.skipif(
    not (CHART_DIR / "values.schema.json").exists(), reason="iris-service chart not checked out"
)
DIGEST = "sha256:" + "0" * 64
SEALED = {"name": "vars-r7", "encryptedData": {"DATABASE_URL": "AgBy3i4TQXwNNqdHFlYAXAmLgbm="}}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate(rendered: str) -> dict[str, Any]:
    schema = json.loads((CHART_DIR / "values.schema.json").read_text())
    defaults = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    values: dict[str, Any] = json.loads(rendered)
    jsonschema.validate(_merge(defaults, values), schema)
    return values


def _app_values(networking: NetworkingValues | None) -> str:
    return render_service_values(
        host_label="api-12",
        release_id=7,
        image_repository="123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
        image_digest=DIGEST,
        source_sha="a" * 40,
        builder=Builder.DOCKERFILE,
        deploy=DeployConfig(),
        base_domain="likelion.uk",
        iris={"serviceName": "api", "targetName": "aws", "deploymentId": 9},
        variables=SEALED,
        scaling=ScalingConfig.defaults(),
        networking=networking,
    )


@requires_chart
def test_app_in_stack_values_pass_chart_schema() -> None:
    values = _validate(
        _app_values(
            NetworkingValues(
                project_id="3",
                container_port=3000,
                host_aliases=(
                    {"name": "postgres", "target": "app.svc-10.svc.cluster.local"},
                    {"name": "redis", "target": "app.svc-11.svc.cluster.local"},
                ),
            )
        )
    )
    assert values["projectId"] == "3"
    assert values["containerPort"] == 3000
    assert values["service"] == {"exposeContainerPort": True}
    assert values["hostAliases"][0] == {
        "name": "postgres",
        "target": "app.svc-10.svc.cluster.local",
    }


@requires_chart
def test_standalone_app_with_networking_omits_empty_host_aliases() -> None:
    values = _validate(_app_values(NetworkingValues(project_id="3")))
    assert "hostAliases" not in values
    assert values["containerPort"] == 8080


def test_app_values_without_networking_have_no_chart_080_keys() -> None:
    values = json.loads(_app_values(None))
    assert not {"projectId", "service", "hostAliases", "workload", "database"} & set(values)
    assert values["containerPort"] == 8080


@requires_chart
@pytest.mark.parametrize("engine", list(DatabaseEngine))
def test_database_values_pass_chart_schema_for_every_engine(engine: DatabaseEngine) -> None:
    image = resolve_image(engine, {})
    values = _validate(
        render_database_values(
            release_id=7,
            project_id="3",
            engine=engine,
            image=image.reference,
            storage_gi=5,
            port=ENGINE_SPECS[engine].port,
            iris={"serviceName": "postgres", "targetName": "aws", "deploymentId": 9},
            variables=SEALED,
            replicas=4,
        )
    )
    assert values["workload"] == {"kind": "database"}
    assert values["replicas"] == 1
    assert values["database"]["storage"] == "5Gi"
    assert not {"image", "command", "route", "health"} & set(values)


@requires_chart
def test_database_values_with_mismatched_image_fail_chart_schema() -> None:
    rendered = render_database_values(
        release_id=7,
        project_id="3",
        engine=DatabaseEngine.POSTGRES,
        image=resolve_image(DatabaseEngine.REDIS, {}).reference,
        storage_gi=5,
        port=5432,
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(rendered)


@requires_chart
def test_host_alias_named_app_fails_chart_schema() -> None:
    rendered = _app_values(
        NetworkingValues(
            project_id="3", host_aliases=({"name": "app", "target": "app.svc-1.svc.cluster.local"},)
        )
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(rendered)


def _db_values(engine: DatabaseEngine, init_scripts: list[dict[str, str]] | None) -> str:
    return render_database_values(
        release_id=7,
        project_id="3",
        engine=engine,
        image=resolve_image(engine, {}).reference,
        storage_gi=5,
        port=ENGINE_SPECS[engine].port,
        init_scripts=init_scripts,
    )


@requires_chart
@pytest.mark.parametrize(
    ("engine", "init_scripts"),
    [
        (
            DatabaseEngine.POSTGRES,
            [
                {"name": "00-schema.sql", "content": "CREATE TABLE jobs (id int);\n"},
                {"name": "01-seed.sql.gz", "binaryContent": "H4sIAAAAAAAAAwMAAAAAAAAAAAA="},
            ],
        ),
        (DatabaseEngine.MYSQL, [{"name": "00-schema.sql", "content": "SELECT 1;\n"}]),
        (DatabaseEngine.MONGODB, [{"name": "00-mongo-init.js", "content": "db.logs.insert({})"}]),
    ],
)
def test_database_values_with_init_scripts_pass_chart_schema(
    engine: DatabaseEngine, init_scripts: list[dict[str, str]]
) -> None:
    values = _validate(_db_values(engine, init_scripts))
    assert values["database"]["initScripts"] == init_scripts


def test_database_values_without_init_scripts_omit_the_key() -> None:
    assert "initScripts" not in json.loads(_db_values(DatabaseEngine.POSTGRES, []))["database"]


@requires_chart
@pytest.mark.parametrize(
    ("engine", "name"),
    [
        (DatabaseEngine.REDIS, "00-schema.sql"),
        (DatabaseEngine.MONGODB, "00-schema.sql"),
        (DatabaseEngine.POSTGRES, "00-init.js"),
        (DatabaseEngine.POSTGRES, "schema.sql"),
    ],
)
def test_database_values_with_engine_mismatched_init_scripts_fail_chart_schema(
    engine: DatabaseEngine, name: str
) -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate(_db_values(engine, [{"name": name, "content": "x"}]))
