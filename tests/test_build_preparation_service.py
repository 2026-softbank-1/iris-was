import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.exceptions import ExternalError
from app.enums import Builder
from app.schemas.build_preparation import (
    BuildEvidenceSchema,
    BuildSourceArchiveSchema,
    PrepareBuildRequest,
    PrepareBuildResponse,
)
from app.services.build_preparation_service import BuildConfigRequiredError, BuildPreparationService

SOURCE_SHA = "a" * 40
DOCKERFILE = b"FROM scratch\nCOPY asset.bin /asset.bin\n"


class FakeAnalyzerBuildClient:
    def __init__(self, result: PrepareBuildResponse) -> None:
        self.result = result
        self.requests: list[PrepareBuildRequest] = []

    async def prepare_build(self, request: PrepareBuildRequest) -> PrepareBuildResponse:
        self.requests.append(request)
        return self.result


def get_request(tmp_path: Path) -> PrepareBuildRequest:
    source = tmp_path / "source"
    source.mkdir()
    (source / "web").mkdir()
    (source / "web" / "Dockerfile").write_bytes(DOCKERFILE)
    return PrepareBuildRequest(
        source_directory=source,
        output_directory=tmp_path / "output",
        source_sha=SOURCE_SHA,
        root_directory="web",
        builder=Builder.DOCKERFILE,
        dockerfile_path="Dockerfile",
    )


def get_response(
    request: PrepareBuildRequest, *, extra_path: str | None = None
) -> PrepareBuildResponse:
    request.output_directory.mkdir(exist_ok=True)
    archive = request.output_directory / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        content = tarfile.TarInfo("source/web/Dockerfile")
        content.size = len(DOCKERFILE)
        stream.addfile(content, io.BytesIO(DOCKERFILE))
        if extra_path is not None:
            content = tarfile.TarInfo(extra_path)
            content.size = 1
            stream.addfile(content, io.BytesIO(b"x"))
    return PrepareBuildResponse(
        schema_version="iris.build-preparation.v1",
        status="ready",
        builder=Builder.DOCKERFILE,
        root_directory="web",
        platform="linux/amd64",
        source_sha=SOURCE_SHA,
        dockerfile_path="Dockerfile",
        dockerfile_origin="source",
        dockerfile_sha256=hashlib.sha256(DOCKERFILE).hexdigest(),
        template_id=None,
        source_manifest_sha256=hashlib.sha256(
            json.dumps(
                [
                    {
                        "path": path.relative_to(request.source_directory).as_posix(),
                        "sizeBytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "executable": bool(path.stat().st_mode & 0o111),
                    }
                    for path in sorted(request.source_directory.rglob("*"))
                    if path.is_file()
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        source_archive=BuildSourceArchiveSchema(
            path=str(archive),
            sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            format="tar.gz",
        ),
    )


async def test_prepare_build_existing_dockerfile_preserves_bytes_and_source_identity(
    tmp_path: Path,
) -> None:
    request = get_request(tmp_path)
    client = FakeAnalyzerBuildClient(get_response(request))
    result = await BuildPreparationService(client).prepare_build(request)
    assert result.source_sha == SOURCE_SHA
    assert result.dockerfile_origin == "source"
    assert result.source_archive is not None
    assert client.requests == [request]
    assert (request.source_directory / "web/Dockerfile").read_bytes() == DOCKERFILE
    assert result.execution_authorized is False


async def test_prepare_build_railpack_explicit_selection_skips_analyzer(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    client = FakeAnalyzerBuildClient(get_response(request))
    result = await BuildPreparationService(client).prepare_build(
        request.model_copy(update={"builder": Builder.RAILPACK})
    )
    assert result.builder == Builder.RAILPACK
    assert result.source_archive is None
    assert result.dockerfile_origin == "railpack"
    assert result.source_sha == SOURCE_SHA
    assert client.requests == []


async def test_prepare_build_unconfirmed_builder_blocks_before_analyzer(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    client = FakeAnalyzerBuildClient(get_response(request))
    with pytest.raises(BuildConfigRequiredError, match="builder selection"):
        await BuildPreparationService(client).prepare_build(
            request.model_copy(update={"builder": None})
        )
    assert not client.requests


@pytest.mark.parametrize(
    "update",
    [
        {"source_sha": "c" * 40},
        {"platform": "linux/arm64"},
        {"root_directory": "."},
        {"dockerfile_path": "Otherfile"},
        {"dockerfile_origin": "controlled_template", "template_id": "fake"},
        {"dockerfile_sha256": "0" * 64},
    ],
)
async def test_prepare_build_changed_identity_or_dockerfile_rejected(
    tmp_path: Path, update: dict[str, object]
) -> None:
    request = get_request(tmp_path)
    client = FakeAnalyzerBuildClient(get_response(request).model_copy(update=update))
    with pytest.raises(ExternalError):
        await BuildPreparationService(client).prepare_build(request)


@pytest.mark.parametrize(
    "path",
    ["../outside", "source/../../outside", "source/web/Dockerfile", "source//web/Dockerfile"],
)
async def test_prepare_build_traversal_and_duplicate_archive_entries_rejected(
    tmp_path: Path, path: str
) -> None:
    request = get_request(tmp_path)
    client = FakeAnalyzerBuildClient(get_response(request, extra_path=path))
    with pytest.raises(ExternalError, match="invalid source entries"):
        await BuildPreparationService(client).prepare_build(request)


async def test_prepare_build_corrupt_archive_hash_rejected(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    response = get_response(request)
    assert response.source_archive is not None
    await asyncio.to_thread(Path(response.source_archive.path).write_bytes, b"replaced")
    with pytest.raises(ExternalError, match="digest does not match"):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)


async def test_prepare_build_generated_template_requires_authorized_missing_dockerfile(
    tmp_path: Path,
) -> None:
    request = get_request(tmp_path)
    (request.source_directory / "web/Dockerfile").unlink()
    response = get_response(request).model_copy(
        update={"dockerfile_origin": "controlled_template", "template_id": "node-npm-start.v1"}
    )
    client = FakeAnalyzerBuildClient(response)
    result = await BuildPreparationService(client).prepare_build(request)
    assert result.template_id == "node-npm-start.v1"
    assert not (request.source_directory / "web/Dockerfile").exists()
    with pytest.raises(ExternalError, match="without generation authorization"):
        await BuildPreparationService(client).prepare_build(
            request.model_copy(update={"allow_generation": False})
        )


async def test_prepare_build_needs_input_preserves_unresolved_decision(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    (request.source_directory / "web/Dockerfile").unlink()
    response = get_response(request).model_copy(
        update={
            "status": "needs_input",
            "source_archive": None,
            "dockerfile_sha256": None,
            "unresolved_inputs": ["Unsupported profile"],
        }
    )
    result = await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)
    assert result.status == "needs_input"
    assert result.unresolved_inputs == ["Unsupported profile"]


@pytest.mark.parametrize("path", ["../outside", "/tmp/Dockerfile", "x/../../Dockerfile", "x\\file"])
def test_prepare_build_request_unsafe_source_paths_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValidationError):
        PrepareBuildRequest(
            source_directory=tmp_path,
            output_directory=tmp_path.parent / "output",
            source_sha=SOURCE_SHA,
            root_directory=path,
            builder=Builder.DOCKERFILE,
        )


@pytest.mark.parametrize("mutation", ["omit", "change", "invent_manifest"])
async def test_prepare_build_all_original_source_files_are_bound_to_archive(
    tmp_path: Path, mutation: str
) -> None:
    request = get_request(tmp_path)
    source_file = request.source_directory / "web/app.js"
    source_file.write_bytes(b"original source bytes")
    response = get_response(request)
    assert response.source_archive is not None
    archive = Path(response.source_archive.path)
    if mutation == "invent_manifest":
        response = response.model_copy(update={"source_manifest_sha256": "b" * 64})
    if mutation == "change":

        def replace_archive() -> None:
            with tarfile.open(archive, "w:gz") as content:
                for name, data in [("Dockerfile", DOCKERFILE), ("app.js", b"altered source bytes")]:
                    member = tarfile.TarInfo("source/web/" + name)
                    member.size = len(data)
                    content.addfile(member, io.BytesIO(data))

        await asyncio.to_thread(replace_archive)
        response = response.model_copy(
            update={
                "source_archive": BuildSourceArchiveSchema(
                    path=str(archive),
                    sha256=hashlib.sha256(await asyncio.to_thread(archive.read_bytes)).hexdigest(),
                    format="tar.gz",
                )
            }
        )
    with pytest.raises(ExternalError, match="(?:omitted|changed original|source manifest)"):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)


@pytest.mark.parametrize("directory", ["secrets", ".secrets", "credentials"])
async def test_prepare_build_credential_directories_are_excluded_from_source_identity(
    tmp_path: Path, directory: str
) -> None:
    request = get_request(tmp_path)
    response = get_response(request)
    secret_directory = request.source_directory / directory
    secret_directory.mkdir()
    (secret_directory / "cloudflared-token").write_text("must-not-be-in-image")
    result = await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)
    assert result.source_manifest_sha256 == response.source_manifest_sha256
    assert result.source_archive is not None
    with tarfile.open(result.source_archive.path, "r:gz") as archive:
        assert archive.getnames() == ["source/web/Dockerfile"]


async def test_prepare_build_cli_explicit_railpack_needs_no_analyzer_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.prepare_build import prepare_build

    request = get_request(tmp_path).model_copy(update={"builder": Builder.RAILPACK})
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json())
    exit_code = await prepare_build(request_path, [], 5)
    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["builder"] == "railpack"
    assert response["source_sha"] == SOURCE_SHA
    assert response["source_archive"] is None


async def test_prepare_build_evidence_hash_must_match_original_source(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    response = get_response(request).model_copy(
        update={"evidence": [BuildEvidenceSchema(path="web/Dockerfile", sha256="f" * 64)]}
    )
    with pytest.raises(ExternalError, match="evidence does not match"):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)
