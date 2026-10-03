"""소스 스냅샷의 내용 해시. 수정 에이전트(iris_code_fix_agent)가 같은 규칙으로 다시 계산해 대조한다.

스냅샷을 올릴 때 한 번 계산해 `builds` 에 고정해 둔다. 에이전트가 내려받은 소스가 그때 올린 것과
같은지 확인하는 기준이므로, 받은 파일에서 해시를 역산하는 것으로 대신할 수 없다.

- `archive_sha256`: tar.gz 파일 바이트의 SHA-256.
- `manifest_sha256`: GitHub tarball 의 최상위 폴더 하나를 벗긴 저장소 루트 기준 파일 목록의 해시.
  항목은 `{mode, path, sha256, size}` 이고 경로 순으로 정렬해 키 정렬·공백 없는 JSON(UTF-8)으로
  직렬화한다. mode 는 실행 비트가 하나라도 있으면 `100755`, 없으면 `100644` 다.

에이전트가 받아들이지 않는 아카이브(링크·특수 파일·한도 초과·위험한 경로)는 manifest 를 만들지
않는다(None). 에이전트는 두 해시가 모두 있을 때만 고정된 소스로 취급한다.
"""

import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

# 수정 에이전트의 source.py 한도와 같다. 다르면 에이전트가 거절하는 소스에 해시를 붙이게 된다.
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 1000

_CHUNK = 1024 * 1024
_EXECUTABLE_MODE = "100755"
_REGULAR_MODE = "100644"


@dataclass(frozen=True)
class SnapshotDigests:
    archive_sha256: str
    manifest_sha256: str | None


def compute_snapshot_digests(path: Path) -> SnapshotDigests:
    """tar.gz 스냅샷의 아카이브·manifest 해시. 동기 I/O 이므로 `asyncio.to_thread` 로 부른다."""
    return SnapshotDigests(_archive_sha256(path), _manifest_sha256(path))


def _archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(path: Path) -> str | None:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        return None
    files = _read_files(path)
    if files is None:
        return None
    files = _strip_single_wrapper(files)
    entries = [
        {"path": name, "sha256": sha256, "mode": mode, "size": size}
        for name, (sha256, mode, size) in sorted(files.items())
    ]
    encoded = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_files(path: Path) -> dict[str, tuple[str, str, int]] | None:
    """이름 → (sha256, mode, size). 에이전트가 거절할 구성이면 None 이다."""
    files: dict[str, tuple[str, str, int]] = {}
    seen: set[str] = set()
    directories: set[str] = set()
    expanded = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/") if member.isdir() else member.name
                if not _is_safe_path(name) or name in seen:
                    return None
                seen.add(name)
                if len(seen) > MAX_FILES * 2:
                    return None
                if member.isdir():
                    directories.add(name)
                    continue
                if not member.isreg() or member.size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                    return None
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    return None
                handle = archive.extractfile(member)
                if handle is None:
                    return None
                digest = hashlib.sha256()
                size = 0
                while chunk := handle.read(_CHUNK):
                    size += len(chunk)
                    digest.update(chunk)
                if size != member.size:
                    return None
                mode = _EXECUTABLE_MODE if member.mode & 0o111 else _REGULAR_MODE
                files[name] = (digest.hexdigest(), mode, size)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return None
    if not files or _has_path_collision(files, directories):
        return None
    return files


def _strip_single_wrapper(
    files: dict[str, tuple[str, str, int]],
) -> dict[str, tuple[str, str, int]]:
    """GitHub tarball 은 모든 파일이 최상위 폴더 하나 아래에 있다. 하나뿐일 때 한 번만 벗긴다."""
    prefixes = {name.split("/", 1)[0] for name in files}
    if len(prefixes) == 1 and all("/" in name for name in files):
        return {name.split("/", 1)[1]: value for name, value in files.items()}
    return files


def _is_safe_path(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name:
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    return not (len(name) > 1 and name[1] == ":" and name[0].isalpha())


def _has_path_collision(files: dict[str, tuple[str, str, int]], directories: set[str]) -> bool:
    """파일 이름이 다른 파일·디렉터리의 상위 경로이면 충돌이다."""
    for name in (*files, *directories):
        parts = name.split("/")
        if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
            return True
    return any(directory in files for directory in directories)
