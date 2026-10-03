"""사용자가 올린 소스 아카이브를 검사하며 GitHub tarball 과 같은 모양으로 다시 묶는다.

CodeBuild 의 buildspec 은 스냅샷을 `tar -xz --strip-components=1` 로 푼다. GitHub tarball 은
최상위에 디렉터리 하나가 있지만, 업로드는 소스의 루트가 곧 아카이브의 루트라서 그대로 두면
첫 경로 요소가 잘려 나간다. 그래서 Build Worker 가 항목마다 검사하면서 최상위 디렉터리 아래로
옮겨 담는다. 아카이브는 사용자 입력이므로 이 과정이 방어선이다. 아카이브를 디스크에 풀지 않고
항목을 하나씩 읽어 새 아카이브에 쓴다.

거절하는 것(ArchiveInvalidError):
- 절대 경로, `..` 가 든 경로, 너무 긴 경로
- 일반 파일·디렉터리·심볼릭 링크·하드 링크 외의 항목(장치·FIFO 등)
- 같은 경로의 중복, 파일 아래에 놓인 항목(심볼릭 링크를 통해 밖에 쓰는 수법 포함)
- 절대 경로이거나 아카이브 루트 밖으로 풀리는 심볼릭 링크. 링크 사슬(`a -> b`, `b -> ..`)은
  모든 항목을 읽은 뒤 가상 파일시스템에서 실제 경로 해석처럼 따라가며 확인한다
- 이미 나온 일반 파일을 가리키지 않는 하드 링크

바꿔서 쓰는 것: uid·gid·소유자 이름을 비우고 setuid·setgid·sticky 비트를 지우며, 확장 헤더
(xattr·file capabilities 등)는 옮기지 않는다.

한도(ArchiveTooLargeError): 풀었을 때의 총 크기와 항목 수, 심볼릭 링크 수. 압축 폭탄을 막으려고
읽는 바이트에 예산을 걸어, 확장 헤더가 거대한 아카이브도 메모리를 키우기 전에 끊는다.
"""

import gzip
import tarfile
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast

from app.core.exceptions import ArchiveInvalidError, ArchiveTooLargeError

MAX_PATH_BYTES = 4096
# 항목 하나의 확장 헤더(pax·GNU 긴 이름)가 가질 수 있는 최대 크기. 실제로는 수 KB 다.
MAX_METADATA_BYTES = 1024 * 1024
# 항목마다 헤더·패딩·확장 헤더로 붙는 바이트의 상한 추정치. 전체 읽기 상한을 계산할 때만 쓴다.
_PER_ENTRY_OVERHEAD_BYTES = 8 * 1024
MAX_SYMLINKS = 5000
MAX_SYMLINK_HOPS = 40
OUTPUT_COMPRESS_LEVEL = 1
# tarfile 의 스트림 모드는 RECORDSIZE 단위로 미리 읽는다. 예산에 그만큼 여유를 둔다.
_READ_AHEAD = tarfile.RECORDSIZE

_FILE = "file"
_DIR = "dir"
_SYMLINK = "symlink"
_HARDLINK = "hardlink"
_NON_DIRECTORY_KINDS = frozenset({_FILE, _SYMLINK, _HARDLINK})

# 읽는 도중 아카이브가 깨졌다고 판단하는 예외. 쓰는 쪽 OSError(디스크 가득 등)는 포함하지 않는다.
_CORRUPT_ARCHIVE_ERRORS = (
    tarfile.TarError,
    gzip.BadGzipFile,
    EOFError,
    zlib.error,
    RecursionError,
    UnicodeError,
)


@dataclass(frozen=True)
class ArchiveLimits:
    max_uncompressed_bytes: int
    max_entries: int


@dataclass(frozen=True)
class ArchiveSummary:
    entries: int
    uncompressed_bytes: int


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _BudgetedReader:
    """압축을 푼 스트림에 읽기 예산을 건다. 예산을 넘겨 읽으면 ArchiveTooLargeError 를 던진다.

    예산은 둘이다. 하나는 항목 하나를 읽는 동안의 예산(`arm`)으로, 확장 헤더가 거대해 메모리를
    키우는 것을 막는다. 다른 하나는 전체 상한으로, 확장 헤더를 항목마다 되풀이해 부풀리는 수법을
    막는다.
    """

    def __init__(self, source: _Readable, total_limit: int) -> None:
        self._source = source
        self._budget = 0
        self._total_remaining = total_limit

    def arm(self, budget: int) -> None:
        self._budget = budget

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self._budget -= len(data)
        self._total_remaining -= len(data)
        if self._budget < 0 or self._total_remaining < 0:
            raise ArchiveTooLargeError("archive expands beyond its declared entries")
        return data


class _ArchiveChecker:
    """항목을 하나씩 받아 검사하고, 새 아카이브에 쓸 TarInfo 를 만든다."""

    def __init__(self, root_name: str, limits: ArchiveLimits) -> None:
        self._root_name = root_name
        self.limits = limits
        self._kinds: dict[str, str] = {}
        self._symlinks: dict[str, str] = {}
        self.entries = 0
        self.uncompressed_bytes = 0

    def accept(self, member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """검사를 통과하면 새 아카이브에 쓸 항목을, 쓸 필요가 없으면 None 을 돌려준다."""
        name = _normalize_path(member.name)
        if not name:
            if member.isdir():
                return None  # 아카이브 루트("." 또는 "./") 자체
            raise ArchiveInvalidError("entry has an empty path")
        self._count_entry(name)

        if member.isdir():
            return self._accept_directory(name, member)
        self._check_parents(name)
        if self._kinds.get(name) is not None:
            raise ArchiveInvalidError("duplicate or conflicting entry", path=name)

        if member.isreg():
            self._count_bytes(name, member.size)
            self._kinds[name] = _FILE
            return self._new_info(name, member, tarfile.REGTYPE, size=member.size)
        if member.issym():
            self._check_symlink_target(name, member.linkname)
            self._kinds[name] = _SYMLINK
            self._symlinks[name] = member.linkname
            return self._new_info(name, member, tarfile.SYMTYPE, linkname=member.linkname)
        if member.islnk():
            target = _normalize_path(member.linkname)
            if self._kinds.get(target) != _FILE:
                raise ArchiveInvalidError("hardlink target is not an earlier file", path=name)
            self._kinds[name] = _HARDLINK
            return self._new_info(
                name, member, tarfile.LNKTYPE, linkname=f"{self._root_name}/{target}"
            )
        raise ArchiveInvalidError("unsupported entry type", path=name)

    def finish(self) -> None:
        """모든 항목을 읽은 뒤 심볼릭 링크가 루트 밖으로 풀리지 않는지 확인한다."""
        if self.entries == 0:
            raise ArchiveInvalidError("archive has no entries")
        for link_path in self._symlinks:
            self._resolve_symlink(link_path)

    def root_directory_info(self) -> tarfile.TarInfo:
        info = tarfile.TarInfo(self._root_name)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        return info

    def _accept_directory(self, name: str, member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        self._check_parents(name)
        kind = self._kinds.get(name)
        if kind in _NON_DIRECTORY_KINDS:
            raise ArchiveInvalidError("duplicate or conflicting entry", path=name)
        self._kinds[name] = _DIR
        return self._new_info(name, member, tarfile.DIRTYPE)

    def _count_entry(self, name: str) -> None:
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise ArchiveTooLargeError("archive has too many entries", path=name)

    def _count_bytes(self, name: str, size: int) -> None:
        self.uncompressed_bytes += size
        if self.uncompressed_bytes > self.limits.max_uncompressed_bytes:
            raise ArchiveTooLargeError("archive is too large when extracted", path=name)

    def _check_parents(self, name: str) -> None:
        """부모 경로가 파일·링크이면 거절하고, 아직 모르는 부모는 디렉터리로 기록한다."""
        parts = name.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if self._kinds.get(parent) in _NON_DIRECTORY_KINDS:
                raise ArchiveInvalidError("entry is below a non-directory", path=name)
            self._kinds.setdefault(parent, _DIR)

    def _check_symlink_target(self, name: str, target: str) -> None:
        if not target or len(target.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
            raise ArchiveInvalidError("symlink target is empty or too long", path=name)
        if target.startswith("/"):
            raise ArchiveInvalidError("symlink target is an absolute path", path=name)
        if len(self._symlinks) >= MAX_SYMLINKS:
            raise ArchiveTooLargeError("archive has too many symlinks", path=name)

    def _resolve_symlink(self, link_path: str) -> None:
        """링크를 실제 경로 해석처럼 따라가 루트 밖으로 나가는지 확인한다.

        `..` 는 이미 풀어 낸 경로에서 한 단계 올라간다. 중간에 만나는 다른 링크는 그 대상으로
        바꿔 이어 간다. 글자만 정리하는 방식은 `d/s -> ..` 같은 링크를 지나는 `d/s/..` 를 놓친다.
        """
        stack = link_path.split("/")[:-1]
        pending = deque(self._symlinks[link_path].split("/"))
        hops = 0
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if not stack:
                    raise ArchiveInvalidError(
                        "symlink target leaves the archive root", path=link_path
                    )
                stack.pop()
                continue
            stack.append(part)
            target = self._symlinks.get("/".join(stack))
            if target is not None:
                hops += 1
                if hops > MAX_SYMLINK_HOPS:
                    raise ArchiveInvalidError("symlink loop", path=link_path)
                stack.pop()
                pending.extendleft(reversed(target.split("/")))

    def _new_info(
        self,
        name: str,
        member: tarfile.TarInfo,
        type_: bytes,
        *,
        size: int = 0,
        linkname: str = "",
    ) -> tarfile.TarInfo:
        info = tarfile.TarInfo(f"{self._root_name}/{name}")
        info.type = type_
        info.size = size
        info.linkname = linkname
        info.mode = member.mode & 0o777
        info.mtime = _clamped_mtime(name, member.mtime)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info


def _clamped_mtime(name: str, mtime: float) -> int:
    try:
        return max(0, int(mtime))
    except (ValueError, OverflowError) as exc:  # pax 확장 헤더의 nan·inf
        raise ArchiveInvalidError("entry has an invalid modification time", path=name) from exc


def _normalize_path(raw: str) -> str:
    """`./` 와 중복 슬래시를 정리한다. 절대 경로·`..`·긴 경로는 거절한다."""
    if raw.startswith("/"):
        raise ArchiveInvalidError("entry path is absolute")
    if len(raw.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
        raise ArchiveInvalidError("entry path is too long")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveInvalidError("entry path contains '..'")
    return "/".join(parts)


def repack_source_archive(
    source: Path, target: Path, *, root_name: str, limits: ArchiveLimits
) -> ArchiveSummary:
    """`source`(tar.gz)를 검사하며 `{root_name}/` 아래로 옮겨 `target`(tar.gz)에 쓴다.

    아카이브가 손상됐거나 허용하지 않는 항목이 있으면 ArchiveInvalidError, 한도를 넘으면
    ArchiveTooLargeError 다. 디스크 쓰기 같은 OS 오류는 그대로 올라간다(재시도 대상).
    """
    checker = _ArchiveChecker(root_name, limits)
    try:
        _copy_entries(source, target, checker)
        checker.finish()
    except _CORRUPT_ARCHIVE_ERRORS as exc:
        raise ArchiveInvalidError("archive is not a readable tar.gz") from exc
    return ArchiveSummary(checker.entries, checker.uncompressed_bytes)


def _copy_entries(source: Path, target: Path, checker: _ArchiveChecker) -> None:
    metadata_budget = MAX_METADATA_BYTES + _READ_AHEAD
    total_limit = (
        checker.limits.max_uncompressed_bytes
        + checker.limits.max_entries * _PER_ENTRY_OVERHEAD_BYTES
        + _READ_AHEAD
    )
    with (
        source.open("rb") as raw,
        gzip.GzipFile(fileobj=raw, mode="rb") as unzipped,
        tarfile.open(
            target,
            "w:gz",
            compresslevel=OUTPUT_COMPRESS_LEVEL,
            format=tarfile.PAX_FORMAT,
        ) as writer,
    ):
        reader = _BudgetedReader(unzipped, total_limit)
        # tarfile.open 이 첫 항목의 헤더를 바로 읽으므로 예산을 먼저 건다.
        reader.arm(metadata_budget)
        # 스트림 모드는 뒤로 가지 않아 항목을 한 번에 하나만 들고 있는다. tarfile 은 read 만 쓴다.
        with tarfile.open(fileobj=cast(IO[bytes], reader), mode="r|") as archive:
            writer.addfile(checker.root_directory_info())
            while True:
                reader.arm(metadata_budget)
                member = archive.next()
                if member is None:
                    break
                info = checker.accept(member)
                if info is None:
                    continue
                if info.isreg():
                    reader.arm(_padded(info.size) + _READ_AHEAD)
                    writer.addfile(info, archive.extractfile(member))
                else:
                    writer.addfile(info)
                # TarFile 은 읽고 쓴 항목을 모두 쥐고 있어 항목이 많으면 메모리가 커진다. 스트림으로
                # 한 번만 지나가므로 필요 없다. `members` 는 타입 스텁에 없는 속성이다.
                archive.members.clear()  # type: ignore[attr-defined]
                writer.members.clear()  # type: ignore[attr-defined]


def _padded(size: int) -> int:
    blocks, remainder = divmod(size, tarfile.BLOCKSIZE)
    return (blocks + (1 if remainder else 0)) * tarfile.BLOCKSIZE
