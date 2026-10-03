import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from app.services import source_manifest
from app.services.source_manifest import compute_snapshot_digests


def _write_archive(path: Path, entries: list[tuple[str, bytes, bytes, int]]) -> None:
    """(이름, 종류, 내용, 권한). 종류는 tarfile 타입 바이트다."""
    with tarfile.open(path, mode="w:gz") as archive:
        for name, kind, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if info.size else None)


def _expected_manifest(files: dict[str, tuple[bytes, str]]) -> str:
    """에이전트가 같은 규칙으로 계산하는 값을 이 테스트가 독립적으로 만든다(계약 고정)."""
    entries = [
        {
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": mode,
            "size": len(data),
        }
        for name, (data, mode) in sorted(files.items())
    ]
    encoded = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_digests_strip_github_wrapper_and_match_contract(tmp_path: Path) -> None:
    path = tmp_path / "source.tar.gz"
    _write_archive(
        path,
        [
            ("owner-repo-abc123", tarfile.DIRTYPE, b"", 0o755),
            ("owner-repo-abc123/app.py", tarfile.REGTYPE, b"print(1)\n", 0o644),
            ("owner-repo-abc123/bin/run.sh", tarfile.REGTYPE, b"#!/bin/sh\n", 0o755),
            ("owner-repo-abc123/한글.txt", tarfile.REGTYPE, "내용".encode(), 0o600),
        ],
    )

    digests = compute_snapshot_digests(path)

    assert digests.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert digests.manifest_sha256 == _expected_manifest(
        {
            "app.py": (b"print(1)\n", "100644"),
            "bin/run.sh": (b"#!/bin/sh\n", "100755"),
            "한글.txt": ("내용".encode(), "100644"),
        }
    )


def test_digests_without_wrapper_keep_paths(tmp_path: Path) -> None:
    path = tmp_path / "source.tar.gz"
    _write_archive(
        path,
        [
            ("app.py", tarfile.REGTYPE, b"a", 0o644),
            ("src/b.py", tarfile.REGTYPE, b"b", 0o644),
        ],
    )

    assert compute_snapshot_digests(path).manifest_sha256 == _expected_manifest(
        {"app.py": (b"a", "100644"), "src/b.py": (b"b", "100644")}
    )


@pytest.mark.parametrize(
    "entries",
    [
        [("w/link", tarfile.SYMTYPE, b"", 0o777)],
        [("w/../escape.py", tarfile.REGTYPE, b"x", 0o644)],
        [("/absolute.py", tarfile.REGTYPE, b"x", 0o644)],
        [("w/a", tarfile.REGTYPE, b"x", 0o644), ("w/a/b", tarfile.REGTYPE, b"y", 0o644)],
        [("w/a.py", tarfile.REGTYPE, b"x", 0o644), ("w/a.py", tarfile.REGTYPE, b"y", 0o644)],
    ],
)
def test_unsupported_archive_has_no_manifest_but_keeps_archive_hash(
    tmp_path: Path, entries: list[tuple[str, bytes, bytes, int]]
) -> None:
    path = tmp_path / "source.tar.gz"
    _write_archive(path, entries)

    digests = compute_snapshot_digests(path)

    assert digests.manifest_sha256 is None
    assert digests.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_digests_of_non_archive_has_no_manifest(tmp_path: Path) -> None:
    path = tmp_path / "source.tar.gz"
    path.write_bytes(b"not a tarball")

    assert compute_snapshot_digests(path).manifest_sha256 is None


def test_digests_over_limits_have_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_manifest, "MAX_FILES", 2)
    path = tmp_path / "many.tar.gz"
    _write_archive(path, [(f"w/f{i}", tarfile.REGTYPE, b"x", 0o644) for i in range(3)])
    assert compute_snapshot_digests(path).manifest_sha256 is None

    monkeypatch.setattr(source_manifest, "MAX_FILES", 1000)
    monkeypatch.setattr(source_manifest, "MAX_FILE_BYTES", 4)
    path = tmp_path / "big.tar.gz"
    _write_archive(path, [("w/big", tarfile.REGTYPE, b"12345", 0o644)])
    assert compute_snapshot_digests(path).manifest_sha256 is None
