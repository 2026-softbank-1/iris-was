"""스택·관리형 DB·환경변수 검증의 순수 규칙 단위 테스트(DB 없이)."""

import pytest

from app.core.exceptions import InvalidInputError
from app.enums import DatabaseEngine, ReferenceProperty
from app.models.service_stack import ServiceStack
from app.services.database_engines import (
    MASK,
    build_connection,
    generate_credentials,
    get_engine_spec,
    normalize_identifier,
    resolve_image,
    url_template,
)
from app.services.service_networking import is_valid_alias_name
from app.services.stack_changes import compute_stack_changes
from app.services.stack_service import _orders
from app.services.variable_validation import (
    _is_localhost,
    _parse_address,
    engine_hint,
    is_address_key,
)


def test_orders_puts_databases_first_then_dependents_by_depth() -> None:
    # 1,2 = DB, 3 = api(→1,2), 4 = worker(→2), 5 = web(→3), 6 = 혼자 있는 앱
    order = _orders({1: set(), 2: set(), 3: {1, 2}, 4: {2}, 5: {3}, 6: set()})
    assert order == {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 1}


def test_orders_ignores_cycles() -> None:
    order = _orders({1: {2}, 2: {1}})
    assert set(order) == {1, 2} and max(order.values()) == 2


def test_compute_stack_changes_reports_added_removed_and_changed() -> None:
    baseline = {
        "units": [
            {"id": "api", "port": 3000, "rootDirectory": "api"},
            {"id": "old", "port": 1, "rootDirectory": "old"},
        ],
        "dependencies": [{"id": "postgres"}],
    }
    current = {
        "units": [
            {"id": "api", "port": 3001, "rootDirectory": "api"},
            {"id": "admin", "port": 4000, "rootDirectory": "admin"},
        ],
        "dependencies": [{"id": "redis"}],
    }
    assert compute_stack_changes(baseline, current) == [
        {"type": "UNIT_ADDED", "unitId": "admin"},
        {"type": "UNIT_REMOVED", "unitId": "old"},
        {"type": "UNIT_CHANGED", "unitId": "api", "field": "port", "from": 3000, "to": 3001},
        {"type": "DEPENDENCY_ADDED", "unitId": "redis"},
        {"type": "DEPENDENCY_REMOVED", "unitId": "postgres"},
    ]
    assert compute_stack_changes(current, current) == []


def test_stack_pending_changes_keep_newest_and_clear_on_revert_or_rebase() -> None:
    stack = ServiceStack(analysis_id=1)
    stack.record_pending_changes(5, "b" * 40, [{"type": "UNIT_ADDED", "unitId": "x"}])
    stack.record_pending_changes(4, "c" * 40, [{"type": "UNIT_ADDED", "unitId": "y"}])
    assert stack.pending_changes is not None and stack.pending_analysis_id == 5
    stack.record_pending_changes(6, "d" * 40, [])
    assert stack.pending_changes is None
    stack.record_pending_changes(7, "e" * 40, [{"type": "UNIT_REMOVED", "unitId": "x"}])
    stack.rebase(7)
    assert stack.pending_changes is None and stack.analysis_id == 7


@pytest.mark.parametrize("engine", list(DatabaseEngine))
def test_generate_credentials_uses_engine_variable_names_and_random_passwords(
    engine: DatabaseEngine,
) -> None:
    spec = get_engine_spec(engine)
    first = generate_credentials(engine, "Shop User", "shop-db")
    second = generate_credentials(engine, None, None)
    assert set(first) == spec.managed_keys
    assert first[spec.password_key] != second[spec.password_key]
    assert len(first[spec.password_key]) >= 32
    if spec.user_key:
        assert (first[spec.user_key], second[spec.user_key]) == ("shop_user", "app")


def test_normalize_identifier_rejects_root_and_bad_start() -> None:
    assert normalize_identifier("root", "app") == "app"
    assert normalize_identifier("1abc", "app") == "app"
    assert normalize_identifier("Iris-Demo", "app") == "iris_demo"


def test_connection_urls_per_engine_and_masking() -> None:
    host = "app.svc-9.svc.cluster.local"
    pg = build_connection(
        DatabaseEngine.POSTGRES,
        host,
        5432,
        {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p@ss", "POSTGRES_DB": "d"},
    )
    assert pg.url() == f"postgresql://u:p%40ss@{host}:5432/d"
    assert pg.url(masked=True) == f"postgresql://u:{MASK}@{host}:5432/d"
    assert pg.property(ReferenceProperty.PASSWORD, masked=True) == MASK
    mongo = build_connection(
        DatabaseEngine.MONGODB,
        host,
        27017,
        {
            "MONGO_INITDB_ROOT_USERNAME": "u",
            "MONGO_INITDB_ROOT_PASSWORD": "p",
            "MONGO_INITDB_DATABASE": "d",
        },
    )
    assert mongo.url() == f"mongodb://u:p@{host}:27017/d?authSource=admin"
    redis = build_connection(DatabaseEngine.REDIS, host, 6379, {"REDIS_PASSWORD": "p"})
    assert redis.url() == f"redis://default:p@{host}:6379"
    assert redis.property(ReferenceProperty.DATABASE) is None
    assert url_template(DatabaseEngine.MYSQL, host, 3306, {"user": "u", "database": "d"}) == (
        f"mysql://u:{MASK}@{host}:3306/d"
    )


def test_resolve_image_accepts_only_pinned_official_engine_images() -> None:
    assert resolve_image(DatabaseEngine.REDIS, {}).digest.startswith("sha256:")
    override = "docker.io/library/redis:7.4@sha256:" + "a" * 64
    assert resolve_image(DatabaseEngine.REDIS, {"redis": override}).reference == override
    with pytest.raises(InvalidInputError):
        resolve_image(DatabaseEngine.REDIS, {"redis": "docker.io/library/redis:7"})
    with pytest.raises(InvalidInputError):
        resolve_image(
            DatabaseEngine.REDIS, {"redis": "docker.io/library/postgres:16@sha256:" + "a" * 64}
        )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("DATABASE_URL", True),
        ("REDIS_HOST", True),
        ("MONGO_URI", True),
        ("API_ADDR", True),
        ("BIND_HOST", False),
        ("LISTEN_ADDR", False),
        ("LOG_LEVEL", False),
    ],
)
def test_is_address_key(key: str, expected: bool) -> None:
    assert is_address_key(key) is expected


def test_parse_address_and_localhost_detection() -> None:
    url = _parse_address("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com:5432/x")
    assert url is not None and (url.scheme, url.host) == ("postgresql", "db.example.com")
    host = _parse_address("REDIS_HOST", "redis:6379")
    assert host is not None and host.host == "redis"
    assert _parse_address("API_URL", "/api") is None
    assert _is_localhost("localhost") and _is_localhost("127.0.0.5") and _is_localhost("::1")
    assert _is_localhost("0.0.0.0")
    assert not _is_localhost("10.0.0.1")


def test_engine_hint_and_alias_names() -> None:
    assert engine_hint("REDIS_URL") == DatabaseEngine.REDIS
    assert engine_hint("MONGODB_URI") == DatabaseEngine.MONGODB
    assert engine_hint("DATABASE_URL") is None
    assert is_valid_alias_name("api") and is_valid_alias_name("my-db")
    assert not is_valid_alias_name("app")
    assert not is_valid_alias_name("Bad_Name")
    assert not is_valid_alias_name("1api")
