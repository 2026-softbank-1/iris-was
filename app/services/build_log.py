"""빌드 로그 끝부분을 `builds.log_tail` 에 둘 모양으로 다듬는다.

DB 에 남기기 전에 비밀이 될 수 있는 값(소스 스냅샷 presigned URL 의 서명, 인증 헤더, GitHub 토큰)을
가리고 줄 수·크기를 제한한다. 진단 에이전트도 마스킹하지만 DB 에는 처음부터 가린 값만 둔다.
"""

import re
from datetime import UTC, datetime
from typing import Any

from app.clients.aws_clients import BuildLogTail

# 에이전트 입력 한도(16KiB)에 맞춰 진단이 최근 줄부터 고르므로, 여기서는 넉넉히 남긴다.
MAX_ENTRIES = 200
MAX_TOTAL_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 2_000

_REDACTED = "[REDACTED]"
_REDACTIONS = (
    re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s\"']+"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"),
    re.compile(r"(?i)()\b(?:ghs|ghp|gho|ghu|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)()\bgithub_pat_[A-Za-z0-9_]{20,}"),
)


def redact_log_message(message: str) -> str:
    redacted = message
    for pattern in _REDACTIONS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", redacted)
    return redacted


def to_log_tail(tail: BuildLogTail) -> dict[str, Any]:
    """CloudWatch 에서 읽은 끝부분을 DB 에 둘 JSON 으로 바꾼다. 가장 최근 줄부터 한도만큼 남긴다."""
    kept: list[dict[str, str]] = []
    total_bytes = 0
    is_truncated = tail.is_truncated
    for line in reversed(tail.lines):
        message = redact_log_message(line.message.rstrip("\r\n")[:MAX_MESSAGE_CHARS])
        if not message.strip():
            continue
        size = len(message.encode("utf-8"))
        if len(kept) >= MAX_ENTRIES or total_bytes + size > MAX_TOTAL_BYTES:
            is_truncated = True
            break
        kept.append({"timestamp": _to_iso(line.timestamp_ms), "message": message})
        total_bytes += size
    kept.reverse()
    return {"entries": kept, "is_truncated": is_truncated}


def _to_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat().replace("+00:00", "Z")
