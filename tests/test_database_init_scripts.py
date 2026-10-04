"""DB 초기화 스크립트 규칙: 소스 재확인(경로·크기·해시·한도), 이름 정규화, values 렌더링,
변경 감지."""

import base64
import gzip
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import DatabaseInitScriptsInvalidError
from app.schemas.analysis_gate import AnalysisGateInitScript
from app.services.database_init_scripts import (
    CHANGE_REASON,
    init_script_name,
    init_scripts_change,
    init_scripts_fingerprint,
    read_init_scripts,
    render_init_scripts,
    select_init_scripts,
)
from app.services.stack_changes import compute_stack_changes

SCHEMA = b"CREATE TABLE jobs (id SERIAL PRIMARY KEY);\n"
SEED = b"INSERT INTO jobs DEFAULT VALUES;\n"
MIB = 1024 * 1024


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _row(path: str, content: bytes, order: int, kind: str = "sql", **extra: Any) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "sha256": _sha(content),
        "size": len(content),
        "order": order,
        "supported": True,
        **extra,
    }


def _result(engine: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"dependencies": [{"id": "postgres", "engine": engine, "initScripts": rows}]}


def _write(root: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _supported(result: dict[str, Any]) -> list[bool]:
    return [row["supported"] for row in result["dependencies"][0]["initScripts"]]


@pytest.mark.parametrize(
    ("order", "path", "kind", "expected"),
    [
        (0, "db/schema.sql", "sql", "00-schema.sql"),
        (1, "db/seed data.SQL", "sql", "01-seed_data.sql"),
        (2, "db/dump.sql.gz", "sql.gz", "02-dump.sql.gz"),
        (3, "docker/mongo-init.js", "js", "03-mongo-init.js"),
        # 단일 파일 mount 는 레포 파일명에 확장자가 없을 수 있다(컨테이너 쪽 kind 로 붙인다).
        (4, "db/init", "sql", "04-init.sql"),
        (5, "db/.sql", "sql", "05-script.sql"),
    ],
)
def test_init_script_name_normalizes_to_chart_pattern(
    order: int, path: str, kind: str, expected: str
) -> None:
    assert init_script_name(order, path, kind) == expected


def test_init_script_name_truncates_long_basename_keeping_suffix() -> None:
    name = init_script_name(7, "db/" + "a" * 300 + ".sql.gz", "sql.gz")
    assert len(name) == 100
    assert name.startswith("07-a") and name.endswith(".sql.gz")


def test_read_init_scripts_verifies_and_returns_contents(tmp_path: Path) -> None:
    _write(tmp_path, {"db/schema.sql": SCHEMA, "db/seed.sql": SEED})
    result = _result("postgres", [_row("db/schema.sql", SCHEMA, 0), _row("db/seed.sql", SEED, 1)])

    contents = read_init_scripts(tmp_path, None, result)

    assert contents == {_sha(SCHEMA): SCHEMA, _sha(SEED): SEED}
    assert _supported(result) == [True, True]


@pytest.mark.parametrize(
    ("row_overrides", "root_directory"),
    [
        ({"sha256": "0" * 64}, None),  # 해시 불일치
        ({"size": len(SCHEMA) + 1}, None),  # 크기 불일치
        ({"path": "../db/schema.sql"}, None),  # 경로 이탈
        ({"path": "/db/schema.sql"}, None),  # 절대 경로
        ({"path": "db/./schema.sql"}, None),  # 정규화되지 않은 경로
        ({"kind": "sh"}, None),  # 플랫폼은 .sh 를 실행하지 않는다
        ({"sha256": None}, None),  # 큰 파일(해시 없음)
        ({}, "services/api"),  # 분석 위치 밖
        ({"path": "db/missing.sql"}, None),
    ],
)
def test_read_init_scripts_rejects_unverifiable_rows(
    tmp_path: Path, row_overrides: dict[str, Any], root_directory: str | None
) -> None:
    _write(tmp_path, {"db/schema.sql": SCHEMA})
    result = _result("postgres", [{**_row("db/schema.sql", SCHEMA, 0), **row_overrides}])

    assert read_init_scripts(tmp_path, root_directory, result) == {}
    assert _supported(result) == [False]


def test_read_init_scripts_rejects_symlinked_path(tmp_path: Path) -> None:
    _write(tmp_path, {"real/schema.sql": SCHEMA})
    os.symlink(tmp_path / "real", tmp_path / "db")
    result = _result("postgres", [_row("db/schema.sql", SCHEMA, 0)])

    assert read_init_scripts(tmp_path, None, result) == {}
    assert _supported(result) == [False]


def test_read_init_scripts_drops_whole_database_over_total_limit(tmp_path: Path) -> None:
    big = b"-- " + b"x" * (MIB // 2 + 10)
    other = b"-- " + b"y" * (MIB // 2 + 10)
    _write(tmp_path, {"db/a.sql": big, "db/b.sql": other})
    result = _result("postgres", [_row("db/a.sql", big, 0), _row("db/b.sql", other, 1)])

    assert read_init_scripts(tmp_path, None, result) == {}
    assert _supported(result) == [False, False]


def test_read_init_scripts_keeps_unsupported_rows_and_engine_kinds(tmp_path: Path) -> None:
    script = b"db.createCollection('logs');\n"
    _write(tmp_path, {"docker/mongo-init.js": script, "docker/init.sh": b"#!/bin/sh\n"})
    result = {
        "dependencies": [
            {
                "id": "mongo",
                "engine": "mongodb",
                "initScripts": [
                    _row("docker/init.sh", b"#!/bin/sh\n", 0, kind="sh", supported=False),
                    _row("docker/mongo-init.js", script, 1, kind="js"),
                ],
            },
            {"id": "redis", "engine": "redis"},
        ]
    }

    assert read_init_scripts(tmp_path, "docker", result) == {_sha(script): script}


def test_select_init_scripts_copies_only_stored_supported_rows_in_order() -> None:
    rows = [
        AnalysisGateInitScript(**_row("db/seed.sql", SEED, 1)),
        AnalysisGateInitScript(**_row("db/schema.sql", SCHEMA, 0)),
        AnalysisGateInitScript(**_row("db/run.sh", b"x", 2, kind="sh", supported=False)),
        AnalysisGateInitScript(**_row("db/lost.sql", b"lost", 3)),
    ]

    selected = select_init_scripts("postgres", rows, {_sha(SCHEMA), _sha(SEED)})

    assert selected == [
        {
            "name": "00-schema.sql",
            "path": "db/schema.sql",
            "kind": "sql",
            "sha256": _sha(SCHEMA),
            "size": len(SCHEMA),
        },
        {
            "name": "01-seed.sql",
            "path": "db/seed.sql",
            "kind": "sql",
            "sha256": _sha(SEED),
            "size": len(SEED),
        },
    ]
    assert select_init_scripts("redis", rows, {_sha(SCHEMA)}) == []


def test_render_init_scripts_uses_content_for_text_and_base64_for_binary() -> None:
    gz = gzip.compress(SCHEMA)
    latin = b"INSERT INTO t VALUES ('\xe9');\n"
    scripts = [
        {"name": "00-schema.sql", "kind": "sql", "sha256": _sha(SCHEMA)},
        {"name": "01-dump.sql.gz", "kind": "sql.gz", "sha256": _sha(gz)},
        {"name": "02-latin.sql", "kind": "sql", "sha256": _sha(latin)},
    ]

    rendered = render_init_scripts(
        scripts, {_sha(SCHEMA): SCHEMA, _sha(gz): gz, _sha(latin): latin}
    )

    assert rendered == [
        {"name": "00-schema.sql", "content": SCHEMA.decode()},
        {"name": "01-dump.sql.gz", "binaryContent": base64.b64encode(gz).decode()},
        {"name": "02-latin.sql", "binaryContent": base64.b64encode(latin).decode()},
    ]


def test_render_init_scripts_rejects_configmap_overflow_and_missing_content() -> None:
    a, b = b"a" * (MIB // 2 + 1), b"b" * (MIB // 2 + 1)
    scripts = [
        {"name": "00-a.sql", "kind": "sql", "sha256": _sha(a)},
        {"name": "01-b.sql", "kind": "sql", "sha256": _sha(b)},
    ]
    with pytest.raises(DatabaseInitScriptsInvalidError):
        render_init_scripts(scripts, {_sha(a): a, _sha(b): b})
    with pytest.raises(DatabaseInitScriptsInvalidError):
        render_init_scripts(scripts[:1], {})
    with pytest.raises(DatabaseInitScriptsInvalidError):
        render_init_scripts(scripts[:1], {_sha(a): b"tampered"})


def test_init_scripts_change_reports_dependency_changed_with_message() -> None:
    before = init_scripts_fingerprint([_row("db/seed.sql", SEED, 0)])
    after = init_scripts_fingerprint([_row("db/seed.sql", SEED + b"-- v2\n", 0)])

    change = init_scripts_change("postgres", before, after, service_id=4)

    assert change is not None
    assert (change["type"], change["unitId"], change["reason"], change["serviceId"]) == (
        "DEPENDENCY_CHANGED",
        "postgres",
        CHANGE_REASON,
        4,
    )
    assert "not re-initialized" in change["message"]
    assert init_scripts_change("postgres", before, before) is None


def test_compute_stack_changes_detects_init_script_changes_only_for_supported_rows() -> None:
    baseline = _result("postgres", [_row("db/schema.sql", SCHEMA, 0)])
    unsupported_added = _result(
        "postgres",
        [
            _row("db/schema.sql", SCHEMA, 0),
            _row("db/x.sh", b"x", 1, kind="sh", supported=False),
        ],
    )
    changed = _result("postgres", [_row("db/schema.sql", SCHEMA + b"-- v2\n", 0)])

    assert compute_stack_changes(baseline, unsupported_added) == []
    changes = compute_stack_changes(baseline, changed)
    assert [(c["type"], c["unitId"], c["field"], c["reason"]) for c in changes] == [
        ("DEPENDENCY_CHANGED", "postgres", "initScripts", CHANGE_REASON)
    ]
    assert changes[0]["to"] == [{"path": "db/schema.sql", "sha256": _sha(SCHEMA + b"-- v2\n")}]
