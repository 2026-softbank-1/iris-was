#!/usr/bin/env python3
"""PostToolUse hook: app/models/*.py 가 변경되면 변경 로그에 **한 줄** 기록하고
Claude 에게 db-schema.sql 동기화를 가볍게 환기한다.

- 로그 한 줄: `- <UTC> · <파일> (<도구>) · <변경 요약>` 를 changelog 마커 아래 prepend.
  변경 요약은 diff 에서 컬럼 추가/삭제/변경을 식별해 사람이 읽을 한 줄로 적는다.
  식별이 안 되면(제약·docstring 등) `+N −M` 줄 수로 폴백한다. 로그는 짧게 유지한다.
- 스키마 동기화: 컬럼/타입/제약/enum 이 바뀌었으면 Alembic revision 생성과
  db-schema.sql 동기화를 환기.

stdlib 만 사용한다(프로젝트 의존성 불필요). 어떤 경우에도 exit 0 으로 끝내
편집 흐름을 막지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

CHANGELOG_REL = ".claude/logs/db-schema-changelog.md"
SCHEMA_REL = ".claude/rules/db-schema.sql"
MARKER = "<!-- CHANGELOG-ENTRIES -->"
MODELS_PREFIX = "app/models/"

# SQLAlchemy 컬럼 정의 라인에서 컬럼명을 뽑는다: `    name: Mapped[...] = mapped_column(...)`
_COLUMN_RE = re.compile(r"^[+-]\s*(\w+)\s*:\s*Mapped\[")


def _project_dir(payload: dict) -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return Path(raw).resolve()


def _relative_model_path(file_path: str, project_dir: Path) -> str | None:
    """변경 파일이 app/models/ 하위의 .py 면 프로젝트 기준 상대경로를, 아니면 None."""
    if not file_path:
        return None
    try:
        rel = Path(file_path).resolve().relative_to(project_dir).as_posix()
    except ValueError:
        return None
    if not rel.startswith(MODELS_PREFIX) or not rel.endswith(".py"):
        return None
    if "__pycache__" in rel:
        return None
    return rel


def _line_counts(diff: str) -> str:
    """컬럼 단위 변화를 못 잡았을 때의 폴백 — 추가/삭제 줄 수 `+N −M`."""
    adds = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return f"+{adds} −{dels}"


def _change_summary(rel_path: str, project_dir: Path) -> str:
    """HEAD 대비 변경을 사람이 읽을 한 줄로. 컬럼 추가/삭제/변경을 식별하고,
    식별 불가하면 `+N −M` 줄 수로 폴백한다."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", rel_path],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "diff 수집 실패"
    diff = result.stdout
    if not diff.strip():
        return "신규/동일"

    added: dict[str, None] = {}
    removed: dict[str, None] = {}
    for raw in diff.splitlines():
        if raw.startswith(("+++", "---")):
            continue
        match = _COLUMN_RE.match(raw)
        if match is None:
            continue
        (added if raw[0] == "+" else removed)[match.group(1)] = None

    new_cols = [n for n in added if n not in removed]
    drop_cols = [n for n in removed if n not in added]
    mod_cols = [n for n in added if n in removed]

    parts: list[str] = []
    if new_cols:
        parts.append(f"{', '.join(new_cols)} 컬럼 추가")
    if drop_cols:
        parts.append(f"{', '.join(drop_cols)} 컬럼 삭제")
    if mod_cols:
        parts.append(f"{', '.join(mod_cols)} 컬럼 변경")
    # 컬럼 단위 변화가 안 잡히면(제약·인덱스·docstring 등) 줄 수로 폴백한다.
    return "; ".join(parts) if parts else _line_counts(diff)


def _prepend_line(changelog: Path, line: str) -> bool:
    """MARKER 바로 아래에 한 줄을 끼워 최신 항목이 위로 오게 한다."""
    if not changelog.exists():
        return False
    text = changelog.read_text(encoding="utf-8")
    if MARKER not in text:
        changelog.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")
        return True
    head, _, tail = text.partition(MARKER)
    changelog.write_text(head + MARKER + "\n" + line + "\n" + tail.lstrip("\n"), encoding="utf-8")
    return True


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    tool_name = payload.get("tool_name", "?")
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    project_dir = _project_dir(payload)

    rel_path = _relative_model_path(file_path, project_dir)
    if rel_path is None:
        return  # 모델 파일이 아니면 조용히 종료

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    summary = _change_summary(rel_path, project_dir)
    line = f"- {timestamp} · {rel_path} ({tool_name}) · {summary}"

    _prepend_line(project_dir / CHANGELOG_REL, line)

    context = (
        f"[db-schema hook] 모델 `{rel_path}` 변경됨 — 변경 로그에 한 줄 자동 기록됨. "
        "컬럼/타입/제약/enum 이 바뀌었으면 Alembic revision 을 만들고"
        "(`uv run alembic revision --autogenerate -m ...`, 결과 검토 필수) "
        f"`{SCHEMA_REL}` 을 동기화하세요. 규칙: .claude/rules/db-migration.md"
    )
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # 어떤 예외도 편집 흐름을 막지 않는다
        pass
    sys.exit(0)
