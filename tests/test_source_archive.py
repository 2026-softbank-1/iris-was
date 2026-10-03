"""업로드 아카이브 재패킹·검사 테스트. 경로 이탈·링크 수법·압축 폭탄을 항목으로 직접 구성해 본다."""

import gzip
import io
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.core.exceptions import ArchiveInvalidError, ArchiveTooLargeError
from app.services import source_archive
from app.services.source_archive import ArchiveLimits, repack_source_archive

ROOT = "source"
LIMITS = ArchiveLimits(max_uncompressed_bytes=1024 * 1024, max_entries=1000)


class _FileMember(tarfile.TarInfo):
    """내용을 함께 들고 있는 일반 파일 항목. TarInfo 는 내용을 담지 않는다."""

    content: bytes = b""


def _file(name: str, content: bytes = b"x", mode: int = 0o644) -> _FileMember:
    info = _FileMember(name)
    info.content = content
    info.size = len(content)
    info.mode = mode
    info.mtime = 1_790_000_000
    return info


def _entry(name: str, type_: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.linkname = linkname
    info.mode = 0o755
    return info


def _symlink(name: str, target: str) -> tarfile.TarInfo:
    return _entry(name, tarfile.SYMTYPE, target)


def _write(path: Path, members: Sequence[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            if isinstance(member, _FileMember):
                archive.addfile(member, io.BytesIO(member.content))
            else:
                archive.addfile(member)
    return path


def _repack(
    tmp_path: Path, members: Sequence[tarfile.TarInfo], limits: ArchiveLimits = LIMITS
) -> Path:
    source = _write(tmp_path / "in.tar.gz", members)
    target = tmp_path / "out.tar.gz"
    repack_source_archive(source, target, root_name=ROOT, limits=limits)
    return target


def _names(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def test_repack_places_entries_under_root_directory_like_github_tarball(tmp_path: Path) -> None:
    target = _repack(
        tmp_path,
        [
            _file("Dockerfile", b"FROM scratch"),
            _file("src/app.py", b"print(1)"),
            _entry("docs", tarfile.DIRTYPE),
        ],
    )

    assert _names(target) == ["source", "source/Dockerfile", "source/src/app.py", "source/docs"]
    # buildspec 의 `tar -xz --strip-components=1` 과 같은 결과: 최상위 디렉터리가 벗겨진다.
    with tarfile.open(target, "r:gz") as archive:
        stripped = [m.name.partition("/")[2] for m in archive.getmembers()]
    assert "Dockerfile" in stripped and "src/app.py" in stripped


def test_repack_extracts_cleanly_with_the_strict_data_filter(tmp_path: Path) -> None:
    target = _repack(
        tmp_path,
        [
            _file("a/b/run.sh", b"#!/bin/sh", mode=0o755),
            _symlink("a/latest", "b/run.sh"),
            _file("c.txt", b"c"),
        ],
    )
    destination = tmp_path / "out"

    with tarfile.open(target, "r:gz") as archive:
        archive.extractall(destination, filter="data")

    assert (destination / "source/a/b/run.sh").read_text() == "#!/bin/sh"
    assert (destination / "source/a/latest").is_symlink()


def test_repack_normalizes_dot_slash_prefix_and_root_entry(tmp_path: Path) -> None:
    target = _repack(
        tmp_path,
        [_entry("./", tarfile.DIRTYPE), _file("./Dockerfile"), _file(".//src//main.py")],
    )

    assert _names(target) == ["source", "source/Dockerfile", "source/src/main.py"]


def test_repack_resets_ownership_and_drops_special_mode_bits(tmp_path: Path) -> None:
    info = _file("run", mode=0o4755)
    info.uid, info.gid, info.uname, info.gname = 1000, 1000, "dev", "staff"
    info.pax_headers = {"SCHILY.xattr.security.capability": "AAAA"}

    target = _repack(tmp_path, [info])

    with tarfile.open(target, "r:gz") as archive:
        repacked = archive.getmember("source/run")
    assert (repacked.mode, repacked.uid, repacked.gid, repacked.uname) == (0o755, 0, 0, "")
    assert "SCHILY.xattr.security.capability" not in repacked.pax_headers


def test_repack_keeps_hardlink_to_earlier_file_with_prefixed_target(tmp_path: Path) -> None:
    target = _repack(
        tmp_path, [_file("a.txt", b"same"), _entry("b.txt", tarfile.LNKTYPE, "./a.txt")]
    )

    with tarfile.open(target, "r:gz") as archive:
        link = archive.getmember("source/b.txt")
    assert (link.islnk(), link.linkname) == (True, "source/a.txt")


def test_repack_allows_symlinks_that_stay_inside_the_root(tmp_path: Path) -> None:
    target = _repack(
        tmp_path,
        [
            _file("shared/config.json", b"{}"),
            _symlink("app/config.json", "../shared/config.json"),
            _symlink("current", "app"),
            _symlink("dangling", "missing/file"),
        ],
    )

    assert "source/app/config.json" in _names(target)


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../outside", "a/../../outside", "a/b/../../../x", ".//../x"],
)
def test_repack_rejects_paths_that_escape_the_extraction_directory(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ArchiveInvalidError):
        _repack(tmp_path, [_file(name)])


@pytest.mark.parametrize("type_", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_repack_rejects_device_and_fifo_entries(tmp_path: Path, type_: bytes) -> None:
    with pytest.raises(ArchiveInvalidError, match="unsupported entry type"):
        _repack(tmp_path, [_entry("dev/null", type_)])


def test_repack_rejects_duplicate_entries(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInvalidError, match="duplicate"):
        _repack(tmp_path, [_file("a"), _file("./a")])


def test_repack_rejects_entry_below_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInvalidError, match="below a non-directory"):
        _repack(tmp_path, [_file("a"), _file("a/b")])


def test_repack_rejects_file_replacing_an_implicit_directory(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInvalidError, match="duplicate or conflicting"):
        _repack(tmp_path, [_file("a/b"), _file("a")])


def test_repack_rejects_writing_through_a_symlink(tmp_path: Path) -> None:
    # 고전적인 수법: 링크를 먼저 두고 그 아래에 파일을 쓰면 링크 대상에 쓰게 된다.
    with pytest.raises(ArchiveInvalidError, match="below a non-directory"):
        _repack(tmp_path, [_symlink("link", "dir"), _file("link/evil")])


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("link", "/etc"),
        ("link", "/"),
        ("link", ".."),
        ("link", "a/../.."),
        ("link", "sub/../../x"),
        ("sub/link", "../.."),
        ("sub/link", "../../x"),
        ("a/b/link", "../../../x"),
    ],
)
def test_repack_rejects_symlinks_that_leave_the_root(
    tmp_path: Path, link: str, target: str
) -> None:
    with pytest.raises(ArchiveInvalidError):
        _repack(tmp_path, [_symlink(link, target)])


@pytest.mark.parametrize(
    ("link", "target"), [("sub/link", ".."), ("sub/link", "a/../.."), ("l", ".")]
)
def test_repack_allows_symlinks_that_resolve_to_the_root_itself(
    tmp_path: Path, link: str, target: str
) -> None:
    target_path = _repack(tmp_path, [_symlink(link, target)])

    assert f"source/{link}" in _names(target_path)


def test_repack_rejects_escape_through_a_chain_of_symlinks(tmp_path: Path) -> None:
    # d/s 는 루트를 가리키므로 d/s/.. 는 실제 경로 해석으로 루트의 부모다. 글자만 정리하면 놓친다.
    with pytest.raises(ArchiveInvalidError, match="leaves the archive root"):
        _repack(tmp_path, [_symlink("d/s", ".."), _symlink("t", "d/s/..")])


def test_repack_rejects_symlink_loops(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInvalidError, match="loop"):
        _repack(tmp_path, [_symlink("a", "b"), _symlink("b", "a")])


@pytest.mark.parametrize("linkname", ["/etc/passwd", "../x", "missing.txt", "dir"])
def test_repack_rejects_hardlinks_not_pointing_to_an_earlier_file(
    tmp_path: Path, linkname: str
) -> None:
    # 절대 경로·`..` 는 경로 검사에서, 없는 파일·디렉터리는 하드 링크 검사에서 걸린다.
    with pytest.raises(ArchiveInvalidError, match="hardlink|entry path"):
        _repack(tmp_path, [_entry("dir", tarfile.DIRTYPE), _entry("h", tarfile.LNKTYPE, linkname)])


def test_repack_rejects_empty_archive(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInvalidError, match="no entries"):
        _repack(tmp_path, [])


def test_repack_rejects_too_many_entries(tmp_path: Path) -> None:
    limits = ArchiveLimits(max_uncompressed_bytes=1024 * 1024, max_entries=3)

    with pytest.raises(ArchiveTooLargeError, match="too many entries"):
        _repack(tmp_path, [_file(f"f{i}") for i in range(4)], limits)


def test_repack_rejects_archive_larger_than_limit_when_extracted(tmp_path: Path) -> None:
    limits = ArchiveLimits(max_uncompressed_bytes=1000, max_entries=10)
    source = _write(tmp_path / "in.tar.gz", [_file("a", b"\0" * 600), _file("b", b"\0" * 600)])

    with pytest.raises(ArchiveTooLargeError, match="too large when extracted"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=limits)


def test_repack_stops_before_reading_a_huge_declared_file(tmp_path: Path) -> None:
    # 헤더가 100MB 를 선언한다. 한도(1MB)를 넘으므로 내용을 읽지 않고 끊어야 한다.
    huge = tarfile.TarInfo("big")
    huge.size = 100 * 1024 * 1024
    source = tmp_path / "in.tar.gz"
    source.write_bytes(gzip.compress(huge.tobuf() + b"\0" * 4096))

    with pytest.raises(ArchiveTooLargeError, match="too large when extracted"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)


def test_repack_cuts_oversized_extended_header_before_buffering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_archive, "MAX_METADATA_BYTES", 4096)
    pax = tarfile.TarInfo("PaxHeader")
    pax.type = tarfile.XHDTYPE
    pax.size = 200 * 1024
    source = tmp_path / "in.tar.gz"
    source.write_bytes(gzip.compress(pax.tobuf() + b"0" * pax.size))

    with pytest.raises(ArchiveTooLargeError, match="expands beyond"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)


def test_repack_cuts_fat_extended_headers_with_total_read_limit(tmp_path: Path) -> None:
    # 항목 하나의 확장 헤더 한도(1MB) 안이지만 항목 수·내용 한도로 가늠한 전체 읽기 상한은 넘는다.
    limits = ArchiveLimits(max_uncompressed_bytes=1024, max_entries=3)
    fat = _file("a", b"x")
    fat.pax_headers = {"comment": "x" * 100_000}
    source = tmp_path / "in.tar.gz"
    with tarfile.open(source, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(fat, io.BytesIO(fat.content))

    with pytest.raises(ArchiveTooLargeError, match="expands beyond"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=limits)


def test_repack_rejects_too_many_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_archive, "MAX_SYMLINKS", 2)

    with pytest.raises(ArchiveTooLargeError, match="too many symlinks"):
        _repack(tmp_path, [_symlink(f"l{i}", "x") for i in range(3)])


@pytest.mark.parametrize("payload", [b"", b"not gzip at all", b"\x1f\x8b\x08\x00garbage"])
def test_repack_rejects_data_that_is_not_a_gzip_tar(tmp_path: Path, payload: bytes) -> None:
    source = tmp_path / "in.tar.gz"
    source.write_bytes(payload)

    with pytest.raises(ArchiveInvalidError, match="not a readable"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)


def test_repack_rejects_truncated_archive(tmp_path: Path) -> None:
    full = _write(tmp_path / "full.tar.gz", [_file("a", b"a" * 50_000), _file("b", b"b" * 50_000)])
    source = tmp_path / "in.tar.gz"
    source.write_bytes(full.read_bytes()[:-200])

    with pytest.raises(ArchiveInvalidError, match="not a readable"):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)


def test_repack_rejects_gzip_of_non_tar_content(tmp_path: Path) -> None:
    source = tmp_path / "in.tar.gz"
    source.write_bytes(gzip.compress(b"plain text, not a tar archive" * 100))

    with pytest.raises(ArchiveInvalidError):
        repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)


def test_repack_returns_summary_of_entries_and_bytes(tmp_path: Path) -> None:
    source = _write(tmp_path / "in.tar.gz", [_file("a", b"123"), _file("d/b", b"45")])

    summary = repack_source_archive(source, tmp_path / "out.tar.gz", root_name=ROOT, limits=LIMITS)

    assert (summary.entries, summary.uncompressed_bytes) == (2, 5)


def test_repack_handles_many_small_files_without_keeping_them_all_in_memory(
    tmp_path: Path,
) -> None:
    members = [_file(f"pkg{i % 50}/module{i}.py", b"pass\n") for i in range(3000)]
    limits = ArchiveLimits(max_uncompressed_bytes=10 * 1024 * 1024, max_entries=10_000)

    target = _repack(tmp_path, members, limits)

    assert len(_names(target)) == 3001
