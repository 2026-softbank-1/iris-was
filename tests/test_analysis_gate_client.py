"""분석기 gate CLI subprocess 경계: 요청 변환·자격증명 미상속·출력 상한·타임아웃·오류 정리."""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.clients.analysis_gate_client import (
    AnalysisGateTimeoutError,
    SubprocessAnalysisGateClient,
)
from app.core.exceptions import ExternalError, NotConfiguredError
from app.enums import AnalysisGateDecision, AnalysisGateMode
from app.schemas.analysis_gate import AnalysisGateRequest

SHA = "a" * 40


def _response(**overrides: Any) -> dict[str, Any]:
    return {
        "schemaVersion": "iris.analysis-gate.v1",
        "sourceSha": SHA,
        "rootDirectory": ".",
        "decision": "analyze",
        "complexity": "complex",
        "reasons": [{"code": "compose_multi_build", "message": "m", "paths": ["compose.yaml"]}],
        "signals": {"dockerfiles": ["api/Dockerfile", "web/Dockerfile"]},
        "simpleBuild": None,
        "units": [
            {
                "id": "api",
                "name": "api",
                "rootDirectory": "api",
                "builder": "dockerfile",
                "dockerfilePath": "Dockerfile",
                "port": 3000,
                "startCommand": None,
                "buildCommand": None,
                "role": "api",
                "public": True,
                "env": [{"key": "DATABASE_URL", "stage": "runtime", "required": True}],
                "dependsOn": ["postgres"],
                "evidence": [{"path": "compose.yaml", "line": 3}],
            }
        ],
        "dependencies": [{"id": "postgres", "engine": "postgres", "image": "postgres:16"}],
        "questions": [],
        "analysis": {"engine": "static", "durationMs": 3, "modelCalls": 0},
        "executionAuthorized": False,
        **overrides,
    }


async def _wait_until(condition: Callable[[], bool], seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not met in time")


def _request(tmp_path: Path, mode: AnalysisGateMode = AnalysisGateMode.AUTO) -> AnalysisGateRequest:
    return AnalysisGateRequest(
        source_root=tmp_path, root_directory="apps", source_sha=SHA, mode=mode
    )


def _program(tmp_path: Path, code: str) -> list[str]:
    script = tmp_path / "analyzer.py"
    script.write_text(code)
    return [sys.executable, str(script), "--request-stdin"]


def _printing(tmp_path: Path, response: object) -> list[str]:
    return _program(tmp_path, f"import sys\nsys.stdout.write({json.dumps(json.dumps(response))})\n")


async def test_analyze_translates_request_and_does_not_inherit_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-forward-this")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "never-forward-that")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    command = _program(
        tmp_path,
        "import json, os, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert request == {'schemaVersion': 'iris.analysis-gate-request.v1',"
        f" 'sourceRoot': {str(tmp_path)!r}, 'rootDirectory': 'apps', 'sourceSha': {SHA!r},"
        " 'mode': 'force', 'ai': False}, request\n"
        "for key in ('AWS_SECRET_ACCESS_KEY', 'GITHUB_APP_PRIVATE_KEY', 'DATABASE_URL'):\n"
        "    assert key not in os.environ, key\n"
        f"sys.stdout.write({json.dumps(json.dumps(_response()))})\n",
    )

    response = await SubprocessAnalysisGateClient(command).analyze(
        _request(tmp_path, AnalysisGateMode.FORCE)
    )

    assert response.result.decision == AnalysisGateDecision.ANALYZE
    assert response.result.units[0].root_directory == "api"
    # 원문은 WAS 가 모르는 필드까지 그대로 남는다.
    assert response.raw["dependencies"][0]["engine"] == "postgres"
    assert response.raw["units"][0]["env"][0]["key"] == "DATABASE_URL"


async def test_analyze_nonzero_exit_hides_child_output(tmp_path: Path) -> None:
    command = _program(
        tmp_path,
        "import sys\nsys.stdout.write('source-secret')\nsys.stderr.write('source-secret')\n"
        "sys.exit(3)\n",
    )

    with pytest.raises(ExternalError) as error:
        await SubprocessAnalysisGateClient(command).analyze(_request(tmp_path))

    assert error.value.fields == {"exit_code": 3, "analyzer_error_code": None}
    assert "source-secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            '{"error": {"code": "GATE_ROOT_DIRECTORY_NOT_FOUND", "message": "/src/x"}}',
            "GATE_ROOT_DIRECTORY_NOT_FOUND",
        ),
        ('{"error": {"code": "not a code /etc/passwd"}}', None),
        ("Traceback (most recent call last)", None),
    ],
    ids=["contract", "unsafe-code", "plain-text"],
)
async def test_analyze_failure_keeps_only_sanitized_error_code(
    tmp_path: Path, stderr: str, code: str | None
) -> None:
    command = _program(tmp_path, f"import sys\nsys.stderr.write({stderr!r} + '\\n')\nsys.exit(2)\n")

    with pytest.raises(ExternalError) as error:
        await SubprocessAnalysisGateClient(command).analyze(_request(tmp_path))

    assert error.value.fields["analyzer_error_code"] == code
    assert "/src/x" not in str(error.value) and "passwd" not in str(error.value)
    if code is not None:
        assert code in error.value.message


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        ["a", "list"],
        {"schemaVersion": "iris.analysis-gate.v0", "decision": "skip", "complexity": "simple"},
        {"schemaVersion": "iris.analysis-gate.v1", "decision": "maybe", "complexity": "simple"},
        _response(executionAuthorized=True),
        _response(units=[{"id": "api"}]),
    ],
    ids=["text", "array", "old-schema", "bad-decision", "authorized", "unit-without-name"],
)
async def test_analyze_invalid_contract_is_rejected(tmp_path: Path, response: object) -> None:
    command = (
        _program(tmp_path, f"import sys\nsys.stdout.write({response!r})\n")
        if isinstance(response, str)
        else _printing(tmp_path, response)
    )

    with pytest.raises(ExternalError, match="invalid response"):
        await SubprocessAnalysisGateClient(command).analyze(_request(tmp_path))


async def test_analyze_other_source_sha_is_rejected(tmp_path: Path) -> None:
    command = _printing(tmp_path, _response(sourceSha="b" * 40))

    with pytest.raises(ExternalError, match="different source"):
        await SubprocessAnalysisGateClient(command).analyze(_request(tmp_path))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_analyze_output_is_bounded(tmp_path: Path, stream: str) -> None:
    command = _program(tmp_path, f"import sys\nsys.{stream}.write('x' * 3_000_000)\n")

    with pytest.raises(ExternalError, match="exceeds the size limit"):
        await SubprocessAnalysisGateClient(command).analyze(_request(tmp_path))


async def test_analyze_timeout_raises_timeout_error(tmp_path: Path) -> None:
    command = _program(tmp_path, "import time\ntime.sleep(20)\n")

    with pytest.raises(AnalysisGateTimeoutError):
        await SubprocessAnalysisGateClient(command, timeout_seconds=0.2).analyze(_request(tmp_path))


async def test_analyze_timeout_kills_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "child-pid"
    command = _program(
        tmp_path,
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
    )

    with pytest.raises(AnalysisGateTimeoutError):
        await SubprocessAnalysisGateClient(command, timeout_seconds=1).analyze(_request(tmp_path))

    child_pid = int(marker.read_text())
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("descendant process survived the timeout")


async def test_analyze_cancel_terminates_process(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    command = _program(
        tmp_path,
        f"import os, pathlib, time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    task = asyncio.create_task(SubprocessAnalysisGateClient(command).analyze(_request(tmp_path)))
    await _wait_until(marker.exists)
    pid = int(marker.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_analyze_missing_executable_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(NotConfiguredError):
        await SubprocessAnalysisGateClient([str(tmp_path / "missing")]).analyze(_request(tmp_path))


@pytest.mark.parametrize("command", [[], [""], ["python\x00"]])
def test_client_rejects_empty_command(command: list[str]) -> None:
    with pytest.raises(NotConfiguredError):
        SubprocessAnalysisGateClient(command)
