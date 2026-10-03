"""Bounded, execution-free snapshot parsing shared with repair handoff clients."""

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass

from app.core.exceptions import InvalidInputError

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 1000


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_path(path: str, *, root: bool = False) -> None:
    if root and path == ".":
        return
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or (len(path) > 1 and path[1] == ":")
    ):
        raise InvalidInputError("unsafe repair source path")


@dataclass(frozen=True)
class RepairSourceFile:
    data: bytes
    mode: str


@dataclass(frozen=True)
class PinnedRepairSource:
    archive_sha256: str
    manifest_sha256: str
    files: dict[str, RepairSourceFile]


def pin_archive(data: bytes, *, strip_wrapper: bool = True) -> PinnedRepairSource:
    archive_digest = sha256(data)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise InvalidInputError("repair source archive exceeds size limit")
    files: dict[str, RepairSourceFile] = {}
    seen: set[str] = set()
    directories: set[str] = set()
    expanded = 0
    try:
        if data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                data = compressed.read(MAX_EXPANDED_BYTES + 4 * 1024 * 1024 + 1)
            if len(data) > MAX_EXPANDED_BYTES + 4 * 1024 * 1024:
                raise ValueError("decompression limit")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/") if member.isdir() else member.name
                validate_path(name)
                if name in seen or len(seen) >= MAX_FILES * 2:
                    raise ValueError("duplicate or entry limit")
                seen.add(name)
                if member.isdir():
                    directories.add(name)
                    continue
                if (
                    member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or member.size > MAX_FILE_BYTES
                    or len(files) >= MAX_FILES
                ):
                    raise ValueError("unsupported entry")
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("expanded limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("missing content")
                content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) != member.size:
                    raise ValueError("invalid size")
                files[name] = RepairSourceFile(
                    content, "100755" if member.mode & 0o111 else "100644"
                )
        for path in files.keys() | directories:
            parts = path.split("/")
            if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                raise ValueError("path collision")
        if files.keys() & directories or not files:
            raise ValueError("empty or colliding source")
    except (ValueError, tarfile.TarError, OSError, EOFError):
        raise InvalidInputError("unsafe or invalid repair source archive") from None
    prefixes = {path.split("/")[0] for path in files}
    if strip_wrapper and len(prefixes) == 1 and all("/" in path for path in files):
        files = {path.split("/", 1)[1]: value for path, value in files.items()}
    manifest = [
        {"path": path, "sha256": sha256(file.data), "mode": file.mode, "size": len(file.data)}
        for path, file in sorted(files.items())
    ]
    return PinnedRepairSource(archive_digest, sha256(canonical_json(manifest)), files)
