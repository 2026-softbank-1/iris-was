"""분석기 gate CLI 를 부르는 비동기 subprocess 경계. 입력·출력 크기와 실행 시간을 묶는다."""

import asyncio
import json
import os
import re
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from app.core.exceptions import ExternalError, NotConfiguredError
from app.schemas.analysis_gate import (
    ANALYSIS_GATE_REQUEST_SCHEMA,
    AnalysisGateRequest,
    AnalysisGateResult,
)

_MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 65536
# 분석기가 실패할 때 stderr 마지막 줄에 남기는 `{"error": {"code": ...}}` 의 코드 모양.
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class AnalysisGateTimeoutError(ExternalError):
    """분석기가 제한 시간 안에 끝나지 않았다. 프로세스 그룹은 이미 정리했다."""


@dataclass(frozen=True)
class AnalysisGateResponse:
    """검증한 결과와 저장할 원문(JSON 객체)."""

    result: AnalysisGateResult
    raw: dict[str, Any]


class AnalysisGateClient(Protocol):
    async def analyze(self, request: AnalysisGateRequest) -> AnalysisGateResponse: ...


class SubprocessAnalysisGateClient:
    """운영자가 설정한 명령으로 분석기를 실행한다. 소스는 명령·인자를 정하지 못한다.

    Worker 의 DB·GitHub·AWS 자격증명을 자식 프로세스에 넘기지 않는다. 실패 시 자식의
    stdout·stderr 는 소스나 비밀을 담을 수 있어 예외에 옮기지 않는다.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 120,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command or any(not item or "\x00" in item for item in command):
            raise NotConfiguredError("analysis gate command is not configured")
        if not 0 < timeout_seconds <= 600:
            raise ValueError("analysis gate timeout must be between zero and 600 seconds")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        self._environment = {"PATH": os.defpath, "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8"}
        if environment is not None:
            self._environment.update(environment)

    async def analyze(self, request: AnalysisGateRequest) -> AnalysisGateResponse:
        payload = json.dumps(
            {
                "schemaVersion": ANALYSIS_GATE_REQUEST_SCHEMA,
                "sourceRoot": str(request.source_root),
                "rootDirectory": request.root_directory,
                "sourceSha": request.source_sha,
                "mode": request.mode.value,
                "ai": False,
            },
            ensure_ascii=False,
        ).encode()
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ExternalError("analysis gate request exceeds the size limit")
        output = await self._run(payload)
        try:
            raw = json.loads(output)
            if not isinstance(raw, dict):
                raise ValueError("response is not an object")
            result = AnalysisGateResult.model_validate(raw)
        except (ValueError, ValidationError):
            raise ExternalError("analysis gate returned an invalid response") from None
        if (
            request.source_sha is not None
            and result.source_sha is not None
            and result.source_sha != request.source_sha
        ):
            raise ExternalError("analysis gate analyzed a different source")
        return AnalysisGateResponse(result=result, raw=raw)

    async def _run(self, payload: bytes) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment,
                # 자식이 띄운 프로세스까지 한 번에 정리하려고 새 프로세스 그룹으로 띄운다.
                start_new_session=True,
            )
        except OSError:
            raise NotConfiguredError("analysis gate executable is unavailable") from None
        tasks: list[asyncio.Task[bytes]] = []
        is_finished = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                assert process.stdin is not None
                assert process.stdout is not None
                assert process.stderr is not None
                tasks = [
                    asyncio.create_task(_read_bounded(process.stdout, MAX_RESPONSE_BYTES)),
                    asyncio.create_task(_read_bounded(process.stderr, _MAX_STDERR_BYTES)),
                ]
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                output, errors = await asyncio.gather(*tasks)
                await process.wait()
                is_finished = True
        except TimeoutError:
            raise AnalysisGateTimeoutError("analysis gate timed out") from None
        except (BrokenPipeError, ConnectionResetError):
            raise ExternalError("analysis gate closed the input stream") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not is_finished:
                await _stop(process)
        if process.returncode:
            code = _find_error_code(errors)
            raise ExternalError(
                f"analysis gate failed ({code})" if code else "analysis gate failed",
                exit_code=process.returncode,
                analyzer_error_code=code,
            )
        return output


def _find_error_code(stderr: bytes) -> str | None:
    """stderr 에서 분석기 오류 코드만 꺼낸다. 메시지는 소스 경로를 담을 수 있어 버린다."""
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        return None
    try:
        error = json.loads(lines[-1]).get("error")
        code = error.get("code") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and _ERROR_CODE_PATTERN.match(code) else None


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > limit:
            raise ExternalError("analysis gate output exceeds the size limit")
    return bytes(data)


async def _stop(process: asyncio.subprocess.Process) -> None:
    # 그룹 리더가 먼저 끝나도 파이프를 쥔 자손이 남을 수 있어 그룹 전체를 죽인다.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(_READ_CHUNK_BYTES):
                pass

    # 읽지 않은 파이프가 차 있으면 자식이 죽은 뒤에도 wait 가 끝나지 않아 비운다.
    await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())
