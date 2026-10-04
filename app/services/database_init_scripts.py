"""관리형 DB 초기화 스크립트(`/docker-entrypoint-initdb.d`) 규칙.

공식 postgres·mysql·mongo 이미지는 데이터 디렉터리가 비어 있는 첫 기동에서만 이 디렉터리의
스크립트를 이름순으로 한 번 실행한다. 플랫폼은 그 동작을 그대로 쓴다.

1. 분석(Build Worker): 분석기가 찾은 `dependencies[].initScripts` 중 supported 인 파일을 풀어 둔
   소스에서 읽어 경로·크기·sha256 을 다시 확인한다. 확인하지 못한 행은 supported=false 로 바꾼다.
2. apply: 새로 만드는 DB 서비스의 `database_config.initScripts` 에 메타데이터(정규화한 이름·경로·
   sha256·크기)를 복사한다. 이미 있는 DB 는 바꾸지 않고 변경만 알린다(다시 실행되지 않는다).
3. 배포(Deploy Worker): sha256 으로 내용을 찾아 values `database.initScripts` 로 렌더링한다.

`.sh` 는 플랫폼이 실행하지 않는다(분석기가 supported=false 로 낸다). 내용은 응답·로그에 내지 않는다.
"""

import base64
import hashlib
import logging
import os
import posixpath
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from app.core.exceptions import DatabaseInitScriptsInvalidError
from app.models.database_init_script import MAX_INIT_SCRIPT_BYTES

logger = logging.getLogger(__name__)

# 파일 하나·DB 하나의 합계 모두 ConfigMap 한도(1 MiB) 안이다. chart 도 20개까지 받는다.
MAX_SCRIPT_BYTES = MAX_INIT_SCRIPT_BYTES
MAX_SCRIPTS_PER_DATABASE = 20
# 분석 1건이 저장할 수 있는 합계(DB 여러 개). 넘는 DB 의 스크립트는 저장하지 않는다.
MAX_BYTES_PER_ANALYSIS = 8 * MAX_INIT_SCRIPT_BYTES
# 엔진별로 플랫폼이 실행하는 종류(분석기 kind 표기). redis 는 초기화 스크립트가 없다.
SUPPORTED_KINDS: Mapping[str, frozenset[str]] = {
    "postgres": frozenset({"sql", "sql.gz"}),
    "mysql": frozenset({"sql", "sql.gz"}),
    "mongodb": frozenset({"js"}),
}
CHANGE_REASON = "init_scripts_changed"
CHANGE_MESSAGE = (
    "Init scripts changed. They run only when the database is first created; "
    "the existing database is not re-initialized."
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LENGTH = 100


def init_script_name(order: int, path: str, kind: str) -> str:
    """values·ConfigMap 키 이름 `{order:02d}-{basename}`. chart 의 이름 규칙에 맞춘다.

    확장자는 kind 로 다시 붙인다(단일 파일 mount 는 레포 파일명과 컨테이너 파일명이 다를 수 있다).
    """
    suffix = f".{kind}"
    basename = posixpath.basename(path)
    stem = basename[: -len(suffix)] if basename.lower().endswith(suffix) else basename
    stem = _NAME_UNSAFE.sub("_", stem).strip("._-") or "script"
    prefix = f"{order:02d}-"
    stem = stem[: _MAX_NAME_LENGTH - len(prefix) - len(suffix)]
    return f"{prefix}{stem}{suffix}"


def read_init_scripts(
    source_root: Path, root_directory: str | None, result: dict[str, Any]
) -> dict[str, bytes]:
    """분석 결과의 supported 초기화 스크립트를 소스에서 읽어 다시 확인한다. {sha256: 내용}.

    확인하지 못한 행(경로 이탈·링크·크기·해시 불일치·한도 초과)은 result 에서 supported=false 로
    바꾼다(apply 가 복사하지 않는다). 소스를 읽기만 하는 동기 함수다(호출 쪽이 스레드로 돌린다).
    """
    contents: dict[str, bytes] = {}
    analysis_bytes = 0
    dependencies = result.get("dependencies")
    for dependency in dependencies if isinstance(dependencies, list) else []:
        if not isinstance(dependency, dict):
            continue
        rows = dependency.get("initScripts")
        if not isinstance(rows, list):
            continue
        kinds = SUPPORTED_KINDS.get(str(dependency.get("engine")), frozenset())
        accepted: list[tuple[dict[str, Any], bytes]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("supported") is not True:
                continue
            content, reason = _read_row(source_root, root_directory, row, kinds)
            if content is None:
                _reject(row, dependency, reason)
                continue
            accepted.append((row, content))
        total = sum(len(content) for _, content in accepted)
        if (
            len(accepted) > MAX_SCRIPTS_PER_DATABASE
            or total > MAX_SCRIPT_BYTES
            or analysis_bytes + total > MAX_BYTES_PER_ANALYSIS
        ):
            # 일부만 넣으면 스키마만 있고 seed 가 없는 식으로 어긋난다. DB 단위로 모두 뺀다.
            for row, _ in accepted:
                _reject(row, dependency, "too_large")
            continue
        analysis_bytes += total
        for row, content in accepted:
            contents[str(row["sha256"])] = content
    return contents


def _read_row(
    source_root: Path, root_directory: str | None, row: Mapping[str, Any], kinds: frozenset[str]
) -> tuple[bytes | None, str]:
    path, sha256, size = row.get("path"), row.get("sha256"), row.get("size")
    if row.get("kind") not in kinds:
        return None, "unsupported_kind"
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        return None, "invalid_sha256"
    if not isinstance(size, int) or not 0 <= size <= MAX_SCRIPT_BYTES:
        return None, "too_large"
    if not isinstance(path, str) or not _is_inside(path, root_directory):
        return None, "invalid_path"
    current = source_root
    info: os.stat_result | None = None
    try:
        for part in path.split("/"):
            current = current / part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return None, "invalid_path"
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return None, "size_mismatch"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(current, flags), "rb") as handle:
            content = handle.read(MAX_SCRIPT_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(content) != size:
        return None, "size_mismatch"
    if hashlib.sha256(content).hexdigest() != sha256:
        return None, "sha256_mismatch"
    return content, ""


def _is_inside(path: str, root_directory: str | None) -> bool:
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        return False
    if posixpath.normpath(path) != path or path == "." or path.split("/")[0] == "..":
        return False
    root = (root_directory or ".").strip("/") or "."
    return root == "." or path.startswith(f"{root}/")


def _reject(row: dict[str, Any], dependency: Mapping[str, Any], reason: str) -> None:
    row["supported"] = False
    logger.warning(
        "init script not verified, skipped",
        extra={
            "action": "read_init_scripts",
            "dependency_id": dependency.get("id"),
            "init_script_path": row.get("path"),
            "reason": reason,
        },
    )


def select_init_scripts(
    engine: str, rows: Iterable[Any], stored_sha256s: set[str]
) -> list[dict[str, Any]]:
    """DB 서비스 `database_config.initScripts` 메타데이터. 확인·저장한 supported 행만 담는다.

    rows 는 분석 결과 `initScripts` 의 행(AnalysisGateInitScript)이다. order 순서다.
    """
    kinds = SUPPORTED_KINDS.get(engine, frozenset())
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.order):
        if not row.supported or row.kind not in kinds or row.sha256 not in stored_sha256s:
            continue
        selected.append(
            {
                "name": init_script_name(row.order, row.path, row.kind),
                "path": row.path,
                "kind": row.kind,
                "sha256": row.sha256,
                "size": row.size,
            }
        )
    return selected[:MAX_SCRIPTS_PER_DATABASE]


def init_scripts_fingerprint(rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """변경 비교 기준: 실행될 스크립트의 경로·sha256 (순서대로). supported=false 행은 뺀다."""
    return [
        {"path": row.get("path"), "sha256": row.get("sha256")}
        for row in rows or []
        if isinstance(row, Mapping) and row.get("supported", True) is True
    ]


def init_scripts_change(
    dependency_id: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    service_id: int | None = None,
) -> dict[str, Any] | None:
    """초기화 스크립트가 달라졌으면 DEPENDENCY_CHANGED 변경 1건. 이미 만든 DB 에는 다시 실행하지
    않는다는 안내를 담는다."""
    if before == after:
        return None
    change: dict[str, Any] = {
        "type": "DEPENDENCY_CHANGED",
        "unitId": dependency_id,
        "field": "initScripts",
        "reason": CHANGE_REASON,
        "message": CHANGE_MESSAGE,
        "from": before,
        "to": after,
    }
    if service_id is not None:
        change["serviceId"] = service_id
    return change


def render_init_scripts(
    scripts: Iterable[Mapping[str, Any]], contents: Mapping[str, bytes]
) -> list[dict[str, str]]:
    """values `database.initScripts`. 텍스트는 content, `.sql.gz`·UTF-8 이 아닌 파일은 base64
    binaryContent 다. ConfigMap 한도(1 MiB)를 넘거나 내용이 없거나 해시가 다르면 거절한다."""
    rendered: list[dict[str, str]] = []
    total = 0
    for script in scripts:
        sha256 = str(script.get("sha256"))
        content = contents.get(sha256)
        if content is None or hashlib.sha256(content).hexdigest() != sha256:
            raise DatabaseInitScriptsInvalidError(
                "database init script content is missing", init_script=script.get("name")
            )
        total += len(content)
        entry: dict[str, str] = {"name": str(script["name"])}
        text = _as_text(content) if script.get("kind") != "sql.gz" else None
        if text is not None:
            entry["content"] = text
        else:
            entry["binaryContent"] = base64.b64encode(content).decode("ascii")
        rendered.append(entry)
    if len(rendered) > MAX_SCRIPTS_PER_DATABASE or total > MAX_INIT_SCRIPT_BYTES:
        raise DatabaseInitScriptsInvalidError(
            "database init scripts exceed the ConfigMap size limit",
            script_count=len(rendered),
            size_bytes=total,
        )
    return rendered


def _as_text(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None
