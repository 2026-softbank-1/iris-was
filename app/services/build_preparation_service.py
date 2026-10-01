"""Prepare a source artifact for the Build Worker without starting a cloud build."""

import asyncio
import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

from app.clients.analyzer_build_client import AnalyzerBuildClient
from app.core.exceptions import ExternalError, InvalidInputError, NotConfiguredError
from app.enums import Builder
from app.schemas.build_preparation import PrepareBuildRequest, PrepareBuildResponse

_MAX_DOCKERFILE_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 60000
_MAX_SOURCE_FILES = 20000
_MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
_EXCLUDED_DIRECTORIES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".aws",
    ".ssh",
    ".azure",
    ".kube",
    "secrets",
    ".secrets",
    "credentials",
}
_CREDENTIAL_FILES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
}


type SourceFiles = dict[str, tuple[int, str, bool]]


def _is_excluded(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    return (
        any(part in _EXCLUDED_DIRECTORIES for part in parts)
        or name == ".env"
        or name.startswith(".env.")
        or name in _CREDENTIAL_FILES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _manifest_digest(files: SourceFiles) -> str:
    entries = [
        {"path": path, "sizeBytes": size, "sha256": sha256, "executable": executable}
        for path, (size, sha256, executable) in sorted(files.items())
    ]
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class BuildConfigRequiredError(InvalidInputError):
    code = "BUILD_CONFIG_REQUIRED"


class BuildPreparationService:
    """Called after fixed-SHA source acquisition and before CodeBuild StartBuild."""

    def __init__(self, analyzer_client: AnalyzerBuildClient | None = None) -> None:
        self._analyzer_client = analyzer_client

    async def prepare_build(self, request: PrepareBuildRequest) -> PrepareBuildResponse:
        try:
            source_files = await asyncio.to_thread(self._get_source_files, request)
            selected = PurePosixPath(
                request.root_directory, request.dockerfile_path or "Dockerfile"
            ).as_posix()
            original = source_files.get(selected)
            original_digest = original[1] if original is not None else None
        except OSError:
            raise BuildConfigRequiredError("source directory cannot be read") from None
        if self._analyzer_client is None:
            raise NotConfiguredError("analyzer command is not configured")
        result = await self._analyzer_client.prepare_build(request)
        self._validate_identity(request, result, original_digest)
        for evidence in result.evidence:
            original_evidence = source_files.get(evidence.path)
            if original_evidence is None or evidence.sha256 != original_evidence[1]:
                raise ExternalError("analyzer evidence does not match the fixed source input")
        if result.status == "needs_input":
            return result
        await asyncio.to_thread(self._validate_artifact, request, result, source_files)
        return result

    @staticmethod
    def _get_source_files(request: PrepareBuildRequest) -> SourceFiles:
        source = request.source_directory.resolve(strict=True)
        output = request.output_directory.resolve()
        if source == output or source in output.parents:
            raise BuildConfigRequiredError(
                "preparation output must be outside the source directory"
            )
        root = source / request.root_directory
        if not root.is_dir() or root.resolve() != root:
            raise BuildConfigRequiredError("service root must be an ordinary source directory")
        files: SourceFiles = {}
        total_bytes = 0
        for current, directories, names in os.walk(source, followlinks=False):
            for name in list(directories):
                path = Path(current) / name
                relative = path.relative_to(source).as_posix()
                if _is_excluded(relative):
                    directories.remove(name)
                elif path.is_symlink():
                    raise BuildConfigRequiredError("source links require explicit packaging")
            for name in names:
                path = Path(current) / name
                relative = path.relative_to(source).as_posix()
                if _is_excluded(relative):
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SOURCE_FILE_BYTES:
                        raise BuildConfigRequiredError("source file exceeds the packaging policy")
                    hasher = hashlib.sha256()
                    size = 0
                    while chunk := stream.read(65536):
                        size += len(chunk)
                        if size > _MAX_SOURCE_FILE_BYTES:
                            raise BuildConfigRequiredError(
                                "source file exceeds the packaging policy"
                            )
                        hasher.update(chunk)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise BuildConfigRequiredError("source changed during preparation")
                total_bytes += size
                if total_bytes > _MAX_ARCHIVE_BYTES or len(files) >= _MAX_SOURCE_FILES:
                    raise BuildConfigRequiredError("source exceeds the packaging policy")
                files[relative] = (size, hasher.hexdigest(), bool(before.st_mode & 0o111))
        return files

    @staticmethod
    def _validate_identity(
        request: PrepareBuildRequest,
        result: PrepareBuildResponse,
        original_digest: str | None,
    ) -> None:
        handoff = result.build_handoff
        if (
            result.source_sha != request.source_sha
            or result.root_directory != request.root_directory
            or result.platform != request.platform
            or handoff.requested_builder != request.builder
            or handoff.recommended_builder != result.builder
            or handoff.decision_required
            != (request.builder is None or result.status == "needs_input")
            or (request.builder is not None and result.builder != request.builder)
        ):
            raise ExternalError("analyzer preparation changed the fixed build identity")
        if result.builder == Builder.RAILPACK:
            if (
                result.status != "ready"
                or result.dockerfile_path is not None
                or result.dockerfile_origin is not None
                or result.dockerfile_sha256 is not None
                or handoff.reason_code
                != (
                    "explicit_railpack"
                    if request.builder == Builder.RAILPACK
                    else "dockerfile_absent"
                )
                or (request.builder is None and original_digest is not None)
            ):
                raise ExternalError("analyzer returned inconsistent Railpack advice")
        elif result.status == "ready":
            if (
                result.dockerfile_path != (request.dockerfile_path or "Dockerfile")
                or result.dockerfile_origin != "source"
                or original_digest is None
                or result.dockerfile_sha256 != original_digest
                or handoff.reason_code != "source_dockerfile"
            ):
                raise ExternalError("analyzer preparation changed an existing Dockerfile")
        elif (
            result.dockerfile_path is not None
            or result.dockerfile_origin is not None
            or result.dockerfile_sha256 is not None
            or original_digest is not None
            or handoff.reason_code
            != (
                "explicit_dockerfile_missing"
                if request.builder == Builder.DOCKERFILE
                else "dockerfile_selection_required"
            )
        ):
            raise ExternalError("analyzer returned inconsistent Dockerfile advice")
        if result.template_id is not None:
            raise ExternalError("analyzer generation is not part of build preparation v2")
        if result.status == "needs_input":
            if result.source_archive is not None or not result.unresolved_inputs:
                raise ExternalError("analyzer preparation lacks required unresolved input details")
        elif (
            result.source_archive is None
            or result.source_manifest_sha256 is None
            or result.unresolved_inputs
        ):
            raise ExternalError("analyzer preparation lacks required build artifact provenance")

    @staticmethod
    def _validate_artifact(
        request: PrepareBuildRequest, result: PrepareBuildResponse, source_files: SourceFiles
    ) -> None:
        assert result.source_archive is not None
        archive = Path(result.source_archive.path)
        output = request.output_directory.resolve()
        if (
            not archive.is_absolute()
            or archive.is_symlink()
            or not archive.is_file()
            or output not in archive.resolve().parents
            or archive.resolve() != archive
            or archive.stat().st_size > _MAX_ARCHIVE_BYTES
        ):
            raise ExternalError("analyzer returned an invalid build artifact path")
        hasher = hashlib.sha256()
        with archive.open("rb") as stream:
            while chunk := stream.read(65536):
                hasher.update(chunk)
        if hasher.hexdigest() != result.source_archive.sha256:
            raise ExternalError("analyzer build artifact digest does not match")
        wanted = (
            PurePosixPath("source", request.root_directory, result.dockerfile_path).as_posix()
            if result.dockerfile_path is not None
            else None
        )
        if result.source_manifest_sha256 != _manifest_digest(source_files):
            raise ExternalError("analyzer source manifest does not match the fixed source input")
        found = False
        checked_files: set[str] = set()
        entries: set[str] = set()
        unpacked_bytes = 0
        try:
            with tarfile.open(archive, "r:gz") as content:
                for count, member in enumerate(content, 1):
                    path = PurePosixPath(member.name)
                    unpacked_bytes += member.size
                    if (
                        count > _MAX_ARCHIVE_ENTRIES
                        or unpacked_bytes > _MAX_ARCHIVE_BYTES
                        or path.is_absolute()
                        or ".." in path.parts
                        or "\\" in member.name
                        or not path.parts
                        or path.parts[0] != "source"
                        or path.as_posix() in entries
                        or path.as_posix() != member.name.rstrip("/")
                        or any(ord(character) < 32 for character in member.name)
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ExternalError(
                            "analyzer build artifact contains invalid source entries"
                        )
                    entries.add(path.as_posix())
                    if not member.isfile():
                        continue
                    relative = PurePosixPath(*path.parts[1:]).as_posix()
                    original = source_files.get(relative)
                    if original is None:
                        raise ExternalError(
                            "analyzer artifact added a file absent from the fixed source input"
                        )
                    member_stream = content.extractfile(member)
                    if member_stream is None:
                        raise ExternalError("analyzer artifact source file is unreadable")
                    hasher = hashlib.sha256()
                    while chunk := member_stream.read(65536):
                        hasher.update(chunk)
                    member_digest = hasher.hexdigest()
                    if original is not None:
                        actual = (member.size, member_digest, bool(member.mode & 0o111))
                        if actual != original:
                            raise ExternalError(
                                "analyzer artifact changed original source bytes or mode"
                            )
                        checked_files.add(relative)
                    if member.name == wanted:
                        if member.size > _MAX_DOCKERFILE_BYTES:
                            raise ExternalError("analyzer artifact Dockerfile is invalid")
                        if member_digest != result.dockerfile_sha256:
                            raise ExternalError(
                                "analyzer artifact Dockerfile digest does not match"
                            )
                        found = True
        except (tarfile.TarError, EOFError, OSError):
            raise ExternalError("analyzer build artifact is not a valid source archive") from None
        if checked_files != set(source_files):
            raise ExternalError("analyzer artifact omitted original source files")
        if wanted is not None and not found:
            raise ExternalError("analyzer artifact does not contain the selected Dockerfile")
