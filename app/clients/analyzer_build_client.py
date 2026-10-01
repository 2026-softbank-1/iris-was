"""Bounded, asynchronous JSON subprocess boundary to the optional analyzer."""

import asyncio
import json
import os
import signal
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import ValidationError

from app.core.exceptions import ExternalError, NotConfiguredError
from app.schemas.build_preparation import PrepareBuildRequest, PrepareBuildResponse

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024


class AnalyzerBuildClient(Protocol):
    async def prepare_build(self, request: PrepareBuildRequest) -> PrepareBuildResponse: ...


class SubprocessAnalyzerBuildClient:
    """An operator-configured executable; source never supplies command or flags."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 120,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command or any(not item or "\x00" in item for item in command):
            raise NotConfiguredError("analyzer command is not configured")
        if not 0 < timeout_seconds <= 600:
            raise ValueError("analyzer timeout must be between zero and 600 seconds")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        # Never forward the worker's DB, GitHub, cloud or model credentials implicitly.
        self._environment = {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}
        if environment is not None:
            self._environment.update(environment)

    async def prepare_build(self, request: PrepareBuildRequest) -> PrepareBuildResponse:
        payload = json.dumps(
            {
                "schemaVersion": "iris.build-preparation-request.v1",
                "sourceRoot": str(request.source_directory),
                "outputDirectory": str(request.output_directory),
                "sourceSha": request.source_sha,
                "rootDirectory": request.root_directory,
                "dockerfilePath": request.dockerfile_path,
                "platform": request.platform,
                "builder": request.builder.value if request.builder is not None else None,
                "allowGeneration": request.allow_generation,
            },
            ensure_ascii=False,
        ).encode()
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ExternalError("analyzer preparation request exceeds the size limit")
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment,
                start_new_session=True,
            )
        except OSError:
            raise NotConfiguredError("analyzer executable is unavailable") from None
        tasks: list[asyncio.Task[bytes]] = []
        is_finished = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                assert process.stdin is not None
                assert process.stdout is not None
                assert process.stderr is not None
                tasks = [
                    asyncio.create_task(self._read_bounded(process.stdout, _MAX_RESPONSE_BYTES)),
                    asyncio.create_task(self._read_bounded(process.stderr, _MAX_STDERR_BYTES)),
                ]
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                response, _ = await asyncio.gather(*tasks)
                await process.wait()
                is_finished = True
        except TimeoutError:
            raise ExternalError("analyzer preparation timed out") from None
        except (BrokenPipeError, ConnectionResetError):
            raise ExternalError("analyzer preparation closed the input stream") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not is_finished:
                await self._stop(process)
        if process.returncode:
            # Raw subprocess stdout/stderr can contain source and credentials.
            raise ExternalError("analyzer preparation failed", exit_code=process.returncode)
        try:
            return PrepareBuildResponse.model_validate_json(response)
        except ValidationError:
            raise ExternalError("analyzer preparation returned an invalid response") from None

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
        data = bytearray()
        while chunk := await stream.read(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise ExternalError("analyzer preparation output exceeds the size limit")
        return bytes(data)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        # The helper owns no live deployment. Terminate its entire process group,
        # including descendants that hold a pipe after the group leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        async def discard(stream: asyncio.StreamReader | None) -> None:
            if stream is not None:
                while await stream.read(65536):
                    pass

        # Drain bounded pipe buffers after killing. A paused unread pipe otherwise
        # prevents asyncio Process.wait from completing even after the child dies.
        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())
