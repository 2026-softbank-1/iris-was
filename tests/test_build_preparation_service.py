import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.exceptions import ExternalError, NotConfiguredError
from app.enums import Builder
from app.schemas.build_preparation import (
    BuildEvidenceSchema,
    BuildHandoffSchema,
    BuildSourceArchiveSchema,
    PrepareBuildRequest,
    PrepareBuildResponse,
)
from app.services.build_preparation_service import BuildPreparationService

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
    )


def get_response(
    request: PrepareBuildRequest, *, extra_path: str | None = None
) -> PrepareBuildResponse:
    request.output_directory.mkdir(exist_ok=True)
    archive = request.output_directory / "source.tar.gz"
    files = [path for path in sorted(request.source_directory.rglob("*")) if path.is_file()]
    with tarfile.open(archive, "w:gz") as stream:
        for path in files:
            content = tarfile.TarInfo(
                "source/" + path.relative_to(request.source_directory).as_posix()
            )
            content.size = path.stat().st_size
            content.mode = path.stat().st_mode & 0o777
            stream.addfile(content, io.BytesIO(path.read_bytes()))
        if extra_path is not None:
            content = tarfile.TarInfo(extra_path)
            content.size = 1
            stream.addfile(content, io.BytesIO(b"x"))
    selected = (
        request.source_directory
        / request.root_directory
        / (request.dockerfile_path or "Dockerfile")
    )
    builder = request.builder or (Builder.DOCKERFILE if selected.exists() else Builder.RAILPACK)
    has_dockerfile = builder == Builder.DOCKERFILE and selected.exists()
    needs_input = builder == Builder.DOCKERFILE and not has_dockerfile
    return PrepareBuildResponse(
        schema_version="iris.build-preparation.v2",
        status="needs_input" if needs_input else "ready",
        builder=builder,
        build_handoff=BuildHandoffSchema(
            owner="service",
            requested_builder=request.builder,
            recommended_builder=builder,
            decision_required=request.builder is None or needs_input,
            reason_code=(
                "source_dockerfile"
                if has_dockerfile
                else "explicit_dockerfile_missing"
                if needs_input
                else "explicit_railpack"
                if request.builder == Builder.RAILPACK
                else "dockerfile_absent"
            ),
        ),
        root_directory=request.root_directory,
        platform=request.platform,
        source_sha=SOURCE_SHA,
        dockerfile_path=(request.dockerfile_path or "Dockerfile") if has_dockerfile else None,
        dockerfile_origin="source" if has_dockerfile else None,
        dockerfile_sha256=hashlib.sha256(selected.read_bytes()).hexdigest()
        if has_dockerfile
        else None,
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
                    for path in files
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        source_archive=None
        if needs_input
        else BuildSourceArchiveSchema(
            path=str(archive),
            sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            format="tar.gz",
        ),
        unresolved_inputs=["Selected Dockerfile missing"] if needs_input else [],
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
    assert result.build_handoff.decision_required is False


async def test_prepare_build_railpack_explicit_selection_preserves_original_dockerfile(
    tmp_path: Path,
) -> None:
    request = get_request(tmp_path).model_copy(update={"builder": Builder.RAILPACK})
    client = FakeAnalyzerBuildClient(get_response(request))
    result = await BuildPreparationService(client).prepare_build(request)
    assert result.builder == Builder.RAILPACK
    assert result.dockerfile_path is result.dockerfile_origin is result.dockerfile_sha256 is None
    assert result.source_archive is not None
    assert client.requests == [request]
    with tarfile.open(result.source_archive.path) as archive:
        stream = archive.extractfile("source/web/Dockerfile")
        assert stream is not None and stream.read() == DOCKERFILE


@pytest.mark.parametrize("has_dockerfile", [True, False])
async def test_prepare_build_unconfirmed_builder_returns_advice_for_service_decision(
    tmp_path: Path,
    has_dockerfile: bool,
) -> None:
    request = get_request(tmp_path).model_copy(update={"builder": None})
    if not has_dockerfile:
        (request.source_directory / "web/Dockerfile").unlink()
        (request.source_directory / "web/app.py").write_text("print('hello')\n")
    client = FakeAnalyzerBuildClient(get_response(request))
    result = await BuildPreparationService(client).prepare_build(request)
    assert result.status == "ready"
    assert result.builder == (Builder.DOCKERFILE if has_dockerfile else Builder.RAILPACK)
    assert result.build_handoff.requested_builder is None
    assert result.build_handoff.decision_required is True
    assert result.build_handoff.owner == "service"
    assert result.execution_authorized is False


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
    "field,value",
    [
        ("requested_builder", None),
        ("recommended_builder", Builder.RAILPACK),
        ("decision_required", True),
        ("reason_code", "dockerfile_absent"),
    ],
)
async def test_prepare_build_inconsistent_handoff_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    request = get_request(tmp_path)
    response = get_response(request)
    response = response.model_copy(
        update={"build_handoff": response.build_handoff.model_copy(update={field: value})}
    )
    with pytest.raises(ExternalError):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)


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


@pytest.mark.parametrize("extra_path", ["source/web/Dockerfile", "source/web/generated.txt"])
async def test_prepare_build_railpack_rejects_added_generated_files(
    tmp_path: Path,
    extra_path: str,
) -> None:
    request = get_request(tmp_path).model_copy(update={"builder": Builder.RAILPACK})
    (request.source_directory / "web/Dockerfile").unlink()
    response = get_response(request, extra_path=extra_path)
    with pytest.raises(ExternalError, match="added a file"):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)


async def test_prepare_build_missing_explicit_dockerfile_does_not_switch_builder(
    tmp_path: Path,
) -> None:
    request = get_request(tmp_path)
    (request.source_directory / "web/Dockerfile").unlink()
    response = get_response(request)
    result = await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)
    assert result.status == "needs_input"
    assert result.builder == Builder.DOCKERFILE
    assert result.build_handoff.decision_required is True
    assert result.build_handoff.reason_code == "explicit_dockerfile_missing"
    assert result.source_archive is None


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
    else:
        with tarfile.open(archive, "w:gz") as content:
            members = [("Dockerfile", DOCKERFILE)]
            if mutation == "change":
                members.append(("app.js", b"altered source bytes"))
            for name, data in members:
                member = tarfile.TarInfo("source/web/" + name)
                member.size = len(data)
                content.addfile(member, io.BytesIO(data))
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


async def test_prepare_build_cli_missing_analyzer_command_cannot_claim_prepared_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.prepare_build import prepare_build

    request = get_request(tmp_path).model_copy(update={"builder": Builder.RAILPACK})
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json())
    exit_code = await prepare_build(request_path, [], 5)
    response = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert response["error"]["code"] == NotConfiguredError.code


async def test_prepare_build_evidence_hash_must_match_original_source(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    response = get_response(request).model_copy(
        update={"evidence": [BuildEvidenceSchema(path="web/Dockerfile", sha256="f" * 64)]}
    )
    with pytest.raises(ExternalError, match="evidence does not match"):
        await BuildPreparationService(FakeAnalyzerBuildClient(response)).prepare_build(request)


def test_prepare_build_v1_generation_fields_rejected(tmp_path: Path) -> None:
    request = get_request(tmp_path)
    with pytest.raises(ValidationError):
        PrepareBuildRequest.model_validate({**request.model_dump(), "allow_generation": True})
    response = get_response(request)
    for changes in [
        {"schema_version": "iris.build-preparation.v1"},
        {"dockerfile_origin": "controlled_template"},
        {"template_id": "node-npm-start.v1"},
        {"execution_authorized": True},
    ]:
        with pytest.raises(ValidationError):
            PrepareBuildResponse.model_validate({**response.model_dump(), **changes})
