"""build target·URL suffix 의 순수 규칙 단위 테스트(DB 없이)."""

import pytest
from pydantic import ValidationError

from app.core.exceptions import BuildFailedError
from app.enums import Builder, DatabaseEngine, ReferenceProperty
from app.models.service import Service
from app.schemas.analysis_gate import AnalysisGateBinding
from app.schemas.service import ServiceUpdateRequest
from app.schemas.variable import VariableCreateRequest
from app.services.builder_detection import detect_builder, parse_iris_config
from app.services.database_engines import DatabaseConnection, build_connection, url_template
from app.services.service_networking import AppConnection
from app.services.stack_apply_service import _binding_reference
from app.services.stack_changes import compute_stack_changes
from app.services.variable_references import VariableReference, reference_from_parts

HOST = "app.svc-1.svc.cluster.local"


def _db(engine: DatabaseEngine, database: str | None) -> DatabaseConnection:
    return build_connection(
        engine,
        HOST,
        5432,
        {"POSTGRES_PASSWORD": "pw", "POSTGRES_USER": "u", "POSTGRES_DB": database or ""},
    )


@pytest.mark.parametrize("target", ["api", "Stage1", "a.b-c_d", "x" * 128])
def test_update_request_accepts_docker_target(target: str) -> None:
    assert ServiceUpdateRequest(dockerTarget=target).docker_target == target  # type: ignore[call-arg]


@pytest.mark.parametrize("target", ["", "-x", "_x", "a b", "a;b", "x" * 129, "a/b"])
def test_update_request_rejects_bad_docker_target(target: str) -> None:
    with pytest.raises(ValidationError):
        ServiceUpdateRequest(dockerTarget=target)  # type: ignore[call-arg]


def test_detect_builder_docker_target_precedence_and_builder_scope() -> None:
    service = Service(builder=Builder.DOCKERFILE, docker_target="api")
    files = {"Dockerfile"}
    assert detect_builder(files, None, service).docker_target == "api"
    config = parse_iris_config(b'{"build": {"dockerTarget": "prod"}}')
    assert detect_builder(files, config, service).docker_target == "prod"
    # railpack 은 target 을 쓰지 않는다.
    railpack = Service(builder=Builder.RAILPACK, docker_target="api")
    assert detect_builder({"package.json"}, None, railpack).docker_target is None
    assert detect_builder(files, None, Service(builder=Builder.DOCKERFILE)).docker_target is None


def test_detect_builder_rejects_invalid_stored_docker_target() -> None:
    with pytest.raises(BuildFailedError):
        detect_builder(
            {"Dockerfile"}, None, Service(builder=Builder.DOCKERFILE, docker_target="a b")
        )
    config = parse_iris_config(b'{"build": {"dockerTarget": "-bad"}}')
    with pytest.raises(BuildFailedError):
        detect_builder({"Dockerfile"}, config, Service(builder=Builder.DOCKERFILE))


def test_stack_changes_reports_build_target_change() -> None:
    base = {"units": [{"id": "api", "buildTarget": "api"}]}
    current = {"units": [{"id": "api", "buildTarget": "worker"}]}
    assert compute_stack_changes(base, current) == [
        {
            "type": "UNIT_CHANGED",
            "unitId": "api",
            "field": "buildTarget",
            "from": "api",
            "to": "worker",
        }
    ]
    assert compute_stack_changes(current, current) == []


@pytest.mark.parametrize(
    "suffix",
    ["/", "/api/v1?tenant=demo", "?x=1", "#frag", "/a?b=c#d", ""],
)
def test_reference_accepts_valid_suffix(suffix: str) -> None:
    reference = VariableReference(service_id=1, property=ReferenceProperty.URL, suffix=suffix)
    assert reference.suffix == (suffix or None)


@pytest.mark.parametrize("suffix", ["api", "/a b", "/a\tb", "/a\nb", "/\x00", "/" + "a" * 2048])
def test_reference_rejects_invalid_suffix(suffix: str) -> None:
    with pytest.raises(ValidationError):
        VariableReference(service_id=1, property=ReferenceProperty.URL, suffix=suffix)


@pytest.mark.parametrize("scheme", ["HTTP", "1x", "", "a b", "a_b", "http://"])
def test_reference_rejects_invalid_scheme(scheme: str) -> None:
    with pytest.raises(ValidationError):
        VariableReference(service_id=1, property=ReferenceProperty.URL, scheme=scheme)


def test_reference_scheme_and_suffix_only_for_url_and_json_omits_empty() -> None:
    with pytest.raises(ValidationError):
        VariableReference(service_id=1, property=ReferenceProperty.HOST, suffix="/x")
    assert VariableReference(service_id=1, property=ReferenceProperty.URL).to_json() == {
        "serviceId": 1,
        "property": "url",
    }
    full = VariableReference(
        service_id=1, property=ReferenceProperty.URL, scheme="postgres+asyncpg", suffix="/d?x=1"
    )
    assert VariableReference.from_json(full.to_json()) == full
    with pytest.raises(ValidationError):
        VariableCreateRequest(
            key="A_HOST",
            reference={"serviceId": 1, "property": "host", "scheme": "http"},  # type: ignore[arg-type]
        )


def test_reference_from_parts_drops_untrusted_values() -> None:
    url = ReferenceProperty.URL
    ok = reference_from_parts(1, url, "http", "/a?b=c")
    assert (ok.scheme, ok.suffix) == ("http", "/a?b=c")
    bad = reference_from_parts(1, url, "HTTP", "no-slash")
    assert (bad.scheme, bad.suffix) == (None, None)
    assert reference_from_parts(1, ReferenceProperty.HOST, "http", "/a").to_json() == {
        "serviceId": 1,
        "property": "host",
    }


def test_binding_reference_maps_scheme_and_suffix_for_url_only() -> None:
    binding = AnalysisGateBinding(
        kind="unit", targetId="api", property="url", scheme="http", urlSuffix="/api/v1?tenant=demo"
    )  # type: ignore[call-arg]
    assert _binding_reference(7, ReferenceProperty.URL, binding) == {
        "serviceId": 7,
        "property": "url",
        "scheme": "http",
        "suffix": "/api/v1?tenant=demo",
    }
    assert _binding_reference(7, ReferenceProperty.HOST, binding) == {
        "serviceId": 7,
        "property": "host",
    }
    legacy = AnalysisGateBinding(kind="unit", targetId="api", property="url")  # type: ignore[call-arg]
    assert _binding_reference(7, ReferenceProperty.URL, legacy) == {
        "serviceId": 7,
        "property": "url",
    }


def test_app_connection_url_uses_scheme_and_suffix_without_credentials() -> None:
    app = AppConnection(HOST, 3000)
    url = ReferenceProperty.URL
    assert app.property(url) == f"http://{HOST}:3000"
    assert app.property(url, scheme="http", suffix="/api/v1?tenant=demo") == (
        f"http://{HOST}:3000/api/v1?tenant=demo"
    )
    assert app.property(url, scheme="ws", suffix="#x") == f"ws://{HOST}:3000#x"
    assert app.property(ReferenceProperty.HOST, suffix="/ignored") == HOST


def test_database_url_keeps_generated_credentials_suffix_path_and_query() -> None:
    conn = _db(DatabaseEngine.POSTGRES, "shop")
    assert conn.url() == f"postgresql://u:pw@{HOST}:5432/shop"
    assert (
        conn.url(suffix="?sslmode=disable") == f"postgresql://u:pw@{HOST}:5432/shop?sslmode=disable"
    )
    assert conn.url(suffix="/other?sslmode=disable#f", scheme="postgres+asyncpg") == (
        f"postgres+asyncpg://u:pw@{HOST}:5432/other?sslmode=disable#f"
    )
    assert conn.url(suffix="/") == f"postgresql://u:pw@{HOST}:5432/shop"
    assert conn.url(masked=True, suffix="/x") == f"postgresql://u:****@{HOST}:5432/x"


def test_mongodb_and_redis_suffix_rules() -> None:
    mongo = build_connection(
        DatabaseEngine.MONGODB,
        HOST,
        27017,
        {"MONGO_INITDB_ROOT_USERNAME": "u", "MONGO_INITDB_ROOT_PASSWORD": "pw"},
    )
    assert mongo.url(suffix="/app?retryWrites=true") == (
        f"mongodb://u:pw@{HOST}:27017/app?retryWrites=true&authSource=admin"
    )
    assert mongo.url(suffix="/app?authSource=other") == (
        f"mongodb://u:pw@{HOST}:27017/app?authSource=other"
    )
    assert mongo.url() == f"mongodb://u:pw@{HOST}:27017?authSource=admin"
    redis = build_connection(DatabaseEngine.REDIS, HOST, 6379, {"REDIS_PASSWORD": "pw"})
    assert redis.url(suffix="/2") == f"redis://default:pw@{HOST}:6379/2"
    assert redis.url(scheme="rediss") == f"rediss://default:pw@{HOST}:6379"


def test_url_template_masks_password_with_suffix() -> None:
    template = url_template(
        DatabaseEngine.POSTGRES,
        HOST,
        5432,
        {"user": "shop", "database": "shop"},
        suffix="?sslmode=disable",
    )
    assert template == f"postgresql://shop:****@{HOST}:5432/shop?sslmode=disable"


def test_reference_user_and_password_variable_rules() -> None:
    ok = VariableReference(
        service_id=3, property=ReferenceProperty.URL, user="archlog", password_variable="APP_PW"
    )
    assert ok.to_json() == {
        "serviceId": 3,
        "property": "url",
        "user": "archlog",
        "passwordVariable": "APP_PW",
    }
    with pytest.raises(ValidationError):
        VariableReference(service_id=3, property=ReferenceProperty.URL, password_variable="X")
    with pytest.raises(ValidationError):
        VariableReference(service_id=3, property=ReferenceProperty.HOST, user="archlog")
    with pytest.raises(ValidationError):
        VariableReference(service_id=3, property=ReferenceProperty.URL, user="bad user")
    dropped = reference_from_parts(
        3, ReferenceProperty.URL, None, None, user="bad user", password_variable="X"
    )
    assert dropped.user is None and dropped.password_variable is None


def test_binding_with_password_secret_maps_user_otherwise_managed_credentials() -> None:
    binding = AnalysisGateBinding.model_validate(
        {
            "kind": "dependency",
            "targetId": "mongo",
            "property": "url",
            "scheme": "mongodb",
            "urlSuffix": "/archlog?authSource=archlog",
            "user": "archlog",
            "passwordSecretId": "MONGO_APP_PASSWORD",
        }
    )
    with_secret = _binding_reference(7, ReferenceProperty.URL, binding, "MONGO_APP_PASSWORD")
    assert with_secret["user"] == "archlog"
    assert with_secret["passwordVariable"] == "MONGO_APP_PASSWORD"
    assert "user" not in _binding_reference(7, ReferenceProperty.URL, binding, None)


def test_app_user_url_skips_admin_auth_source_and_encodes_password() -> None:
    connection = build_connection(
        DatabaseEngine.MONGODB,
        "h",
        27017,
        {
            "MONGO_INITDB_ROOT_USERNAME": "root",
            "MONGO_INITDB_ROOT_PASSWORD": "rootpw",
            "MONGO_INITDB_DATABASE": "app",
        },
    ).with_user("archlog", "p@ss/w")
    assert connection.url(scheme="mongodb", suffix="/archlog") == (
        "mongodb://archlog:p%40ss%2Fw@h:27017/archlog"
    )
    assert url_template(
        DatabaseEngine.MONGODB, "h", 27017, {"user": "root"}, suffix="/archlog", user="archlog"
    ) == ("mongodb://archlog:****@h:27017/archlog")


def test_generated_secret_is_long_enough_for_common_minimums() -> None:
    # 앱이 SESSION_SECRET 등에 48자 이상을 요구한 운영 사례(Temp_log zod 검사) 회귀 방지.
    import secrets

    from app.services import stack_apply_service

    value = secrets.token_urlsafe(stack_apply_service._SECRET_BYTES)
    assert len(value) >= 64
