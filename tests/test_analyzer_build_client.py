import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.clients.analyzer_build_client import SubprocessAnalyzerBuildClient
from app.core.exceptions import ExternalError, NotConfiguredError
from app.enums import Builder
from app.schemas.build_preparation import PrepareBuildRequest


def get_request(tmp_path: Path) -> PrepareBuildRequest:
    return PrepareBuildRequest(
        source_directory=tmp_path,
        output_directory=tmp_path.parent / "output",
        source_sha="a" * 40,
        root_directory="web",
        builder=Builder.DOCKERFILE,
    )


def get_program(tmp_path: Path, code: str) -> list[str]:
    script = tmp_path / "analyzer.py"
    script.write_text(code)
    return [sys.executable, str(script), "--request-stdin"]


@pytest.mark.parametrize("builder", [Builder.DOCKERFILE, None])
async def test_analyzer_client_translates_wire_request_and_does_not_inherit_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, builder: Builder | None
) -> None:
    monkeypatch.setenv("OPENAI_API", "never-forward-this-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-forward-that-secret")
    response = {
        "schemaVersion": "iris.build-preparation.v2",
        "status": "needs_input",
        "builder": "dockerfile",
        "buildHandoff": {
            "owner": "service",
            "requestedBuilder": builder.value if builder is not None else None,
            "recommendedBuilder": "dockerfile",
            "decisionRequired": True,
            "reasonCode": "explicit_dockerfile_missing",
        },
        "rootDirectory": "web",
        "platform": "linux/amd64",
        "sourceSha": "a" * 40,
        "dockerfilePath": None,
        "dockerfileOrigin": None,
        "dockerfileSha256": None,
        "templateId": None,
        "sourceManifestSha256": None,
        "sourceArchive": None,
        "unresolvedInputs": ["Unsupported source profile"],
    }
    command = get_program(
        tmp_path,
        "import json, os, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert request['schemaVersion'] == 'iris.build-preparation-request.v2'\n"
        f"assert request['builder'] == {builder.value if builder is not None else 'auto'!r}\n"
        "assert 'allowGeneration' not in request\n"
        "assert request['sourceSha'] == 'a' * 40\n"
        "assert request['rootDirectory'] == 'web'\n"
        "assert 'OPENAI_API' not in os.environ\n"
        "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
        f"sys.stdout.write({json.dumps(json.dumps(response))})\n",
    )
    request = get_request(tmp_path).model_copy(update={"builder": builder})
    result = await SubprocessAnalyzerBuildClient(command).prepare_build(request)
    assert result.source_sha == "a" * 40
    assert result.status == "needs_input"


async def test_analyzer_client_nonzero_sanitizes_child_output(tmp_path: Path) -> None:
    command = get_program(
        tmp_path,
        "import sys\nsys.stdout.write('api-secret-value')\n"
        "sys.stderr.write('api-secret-value')\nsys.exit(7)\n",
    )
    with pytest.raises(ExternalError) as error:
        await SubprocessAnalyzerBuildClient(command).prepare_build(get_request(tmp_path))
    assert error.value.fields == {"exit_code": 7}
    assert "api-secret-value" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("output", ["not json", '{"executionAuthorized": true}'])
async def test_analyzer_client_invalid_contract_rejected(tmp_path: Path, output: str) -> None:
    command = get_program(tmp_path, f"import sys\nsys.stdout.write({output!r})\n")
    with pytest.raises(ExternalError, match="invalid response"):
        await SubprocessAnalyzerBuildClient(command).prepare_build(get_request(tmp_path))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_analyzer_client_output_size_is_bounded(tmp_path: Path, stream: str) -> None:
    command = get_program(tmp_path, f"import sys\nsys.{stream}.write('x' * 3000000)\n")
    with pytest.raises(ExternalError, match="exceeds the size limit"):
        await SubprocessAnalyzerBuildClient(command).prepare_build(get_request(tmp_path))


async def test_analyzer_client_timeout_terminates_process(tmp_path: Path) -> None:
    command = get_program(tmp_path, "import time\ntime.sleep(20)\n")
    with pytest.raises(ExternalError, match="timed out"):
        await SubprocessAnalyzerBuildClient(command, timeout_seconds=0.05).prepare_build(
            get_request(tmp_path)
        )


async def test_analyzer_client_cancel_terminates_process(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    command = get_program(
        tmp_path,
        f"import os, pathlib, time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(20)\n",
    )
    task = asyncio.create_task(
        SubprocessAnalyzerBuildClient(command).prepare_build(get_request(tmp_path))
    )
    async with asyncio.timeout(2):
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_analyzer_client_missing_executable_has_domain_error(tmp_path: Path) -> None:
    with pytest.raises(NotConfiguredError):
        await SubprocessAnalyzerBuildClient([str(tmp_path / "missing")]).prepare_build(
            get_request(tmp_path)
        )


async def test_analyzer_client_timeout_kills_descendant_after_leader_exit(tmp_path: Path) -> None:
    marker = tmp_path / "child-pid"
    command = get_program(
        tmp_path,
        "import subprocess, sys, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n",
    )
    with pytest.raises(ExternalError, match="timed out"):
        await SubprocessAnalyzerBuildClient(command, timeout_seconds=0.2).prepare_build(
            get_request(tmp_path)
        )
    pid = int(marker.read_text())
    # A killed orphan can be briefly reaped by init asynchronously.
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("analyzer descendant remained alive after timeout")
