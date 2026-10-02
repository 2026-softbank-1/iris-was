"""Worker-only adapter for the pinned, evidence-backed analysis library."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from pydantic import JsonValue, TypeAdapter, ValidationError

from app.core.async_io import run_sync
from app.core.exceptions import AppError, ExternalError, InvalidInputError, NotConfiguredError

type ProgressSink = Callable[[str], Awaitable[None]]
_JSON = TypeAdapter(dict[str, JsonValue])
_SHA = re.compile(r"^[a-f0-9]{40}$")
_SAFE_CALL_FIELDS = frozenset(
    {
        "provider",
        "model",
        "promptVersion",
        "outputMode",
        "serverVersion",
        "apiSchemaHash",
        "latencySeconds",
        "usage",
        "cost",
        "estimatedCostUsd",
        "error",
        "httpAttempts",
        "remoteRetryAttempts",
        "maxOutputTokens",
        "maxRemoteRetries",
        "maxInferenceSteps",
        "snapshotId",
        "contextHash",
        "revision",
        "sessionId",
        "messageId",
        "responseMessageId",
        "modelInputDigest",
        "modelPromptDigest",
        "requestDigest",
        "responseProtocol",
        "contextBinding",
        "finishReason",
        "cumulativeReportedTokens",
        "cumulativeEstimatedCostUsd",
        "remoteAbortConfirmed",
    }
)


class AnalysisExecutionError(AppError):
    code = "ANALYSIS_EXECUTION_FAILED"
    status_code = 502


class AnalyzerClient(Protocol):
    async def analyze(
        self,
        source_root: Path,
        source_sha: str,
        root_directory: str,
        mode: str,
        *,
        on_progress: ProgressSink | None = None,
    ) -> dict[str, JsonValue]: ...


class LocalAnalyzerClient:
    def __init__(
        self,
        *,
        budget_ledger: Path,
        config: dict[str, JsonValue] | None = None,
        executable: str = "opencode",
        max_cost_usd: float = 1,
    ) -> None:
        self._config = dict(config) if config is not None else None
        self._executable = executable
        self._budget_ledger = budget_ledger
        self._max_cost_usd = max_cost_usd
        self._slots = asyncio.Semaphore(1)

    async def analyze(
        self,
        source_root: Path,
        source_sha: str,
        root_directory: str,
        mode: str,
        *,
        on_progress: ProgressSink | None = None,
    ) -> dict[str, JsonValue]:
        if mode not in {"static", "opencode"} or not _SHA.fullmatch(source_sha):
            raise InvalidInputError("invalid analysis execution input")
        root = PurePosixPath(root_directory or ".")
        if (
            root.is_absolute()
            or ".." in root.parts
            or "\\" in root_directory
            or any(ord(char) < 32 for char in root_directory)
        ):
            raise InvalidInputError("analysis root must stay inside the repository")
        await run_sync(lambda: self._validate_source(source_root, root))
        if mode == "opencode" and self._config is None:
            raise NotConfiguredError("analysis model is not configured")
        async with self._slots:
            return await self._analyze(source_root, source_sha, root.as_posix(), mode, on_progress)

    async def _analyze(
        self,
        source_root: Path,
        source_sha: str,
        root_directory: str,
        mode: str,
        on_progress: ProgressSink | None,
    ) -> dict[str, JsonValue]:
        # Control API processes can import this module without the optional worker dependency.
        try:
            from iris_analyzer.contracts import AnalyzerError, digest, validate_result
            from iris_analyzer.deployment.client import plan_async
            from iris_analyzer.deployment.dossier import prepare_readiness
            from iris_analyzer.integrations import (
                AnalysisClientError,
                LocalAnalysisClient,
                create_live_runner_factory,
            )
            from iris_analyzer.opencode import ModelConfig
        except ImportError:
            raise NotConfiguredError("analysis worker package is unavailable") from None

        temporary = await run_sync(lambda: TemporaryDirectory(prefix="iris-analysis-result-"))
        output_root = Path(temporary.name)
        try:
            factory = None
            if mode == "opencode":
                factory = create_live_runner_factory(
                    ModelConfig(**(self._config or {})),
                    budget_ledger=self._budget_ledger,
                    executable=self._executable,
                    max_cost_usd=self._max_cost_usd,
                )
            client = LocalAnalysisClient(runner_factory=factory)

            async def progress(event: Any) -> None:
                if on_progress:
                    await on_progress(str(event.stage))

            # Capture the entire repo, preserving workspace and cross-service relationships.
            outcome = await client.analyze_repository(
                source_root, out=output_root / "analysis", on_progress=progress
            )
            analysis = outcome.analysis_result
            validate_result(analysis)
            verification = outcome.run_report.get("verification")
            if not isinstance(verification, dict) or (
                verification.get("schemaVersion") != "iris.analysis-verification.v1"
                or verification.get("sourceSnapshotId") != analysis["sourceSnapshotId"]
                or verification.get("contextHash") != analysis["contextHash"]
                or verification.get("resultDigest") != digest(analysis)
                or verification.get("deploymentAuthorized") is not False
            ):
                raise ExternalError("analysis verification does not match the source result")
            if on_progress:
                await on_progress("readiness")
            readiness = await run_sync(
                lambda: prepare_readiness(
                    source_root, analysis=analysis, out=output_root / "readiness"
                )
            )
            if readiness["sourceSnapshotId"] != analysis["sourceSnapshotId"]:
                raise ExternalError("analysis readiness source does not match")
            if on_progress:
                await on_progress("planning")
            # Preserve the planner's validated policy and blocked execution state.
            # Planning is deterministic here and does not trigger a second paid model invocation.
            dossier, planning_report = await plan_async(analysis, readiness, config=None)
            evidence = await run_sync(
                lambda: self._read_evidence(output_root / "analysis" / "evidence.jsonl")
            )
            result: dict[str, Any] = {
                "analysisResult": analysis,
                "verificationReport": verification,
                "sourceReadiness": readiness,
                "deploymentDossier": dossier,
                "runReport": self._public_report(outcome.run_report),
                "planningReport": self._public_report(planning_report),
                "sourceSha": source_sha,
                "rootDirectory": root_directory,
                "sourceSnapshotId": analysis["sourceSnapshotId"],
                "contextHash": analysis["contextHash"],
                "resultDigest": digest(analysis),
                "analysisStatus": analysis["status"],
                "analysisMode": mode,
                "builderRecommendation": await run_sync(
                    lambda: (
                        "dockerfile"
                        if (source_root / root_directory / "Dockerfile").is_file()
                        else "railpack"
                    )
                ),
                "evidence": evidence,
                "deploymentAuthorized": False,
            }
            # Enforce finite JSON and nested null preservation at the WAS boundary.
            return _JSON.validate_json(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except (AnalyzerError, AnalysisClientError) as error:
            raise AnalysisExecutionError("repository analysis failed", reason=error.code) from None
        except (OSError, ValueError, TypeError, KeyError, ValidationError):
            raise AnalysisExecutionError("analysis output could not be validated") from None
        finally:
            await run_sync(temporary.cleanup)

    @staticmethod
    def _validate_source(source_root: Path, root: PurePosixPath) -> None:
        if not source_root.is_absolute() or source_root.resolve() != source_root:
            raise InvalidInputError("analysis source must be an ordinary absolute directory")
        selected = source_root.joinpath(*root.parts)
        if not selected.is_dir() or selected.resolve() != selected:
            raise InvalidInputError("analysis service root is unavailable")

    @staticmethod
    def _public_report(report: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            key: report[key]
            for key in (
                "schemaVersion",
                "mode",
                "status",
                "durationSeconds",
                "snapshotId",
                "contextHash",
                "sourceSnapshotId",
                "planDigest",
                "adviceSkippedReason",
                "errors",
                "events",
            )
            if key in report
        }
        result["calls"] = [
            {key: value for key, value in call.items() if key in _SAFE_CALL_FIELDS}
            for call in report.get("calls", [])
            if isinstance(call, dict)
        ]
        return result

    @staticmethod
    def _read_evidence(path: Path) -> list[dict[str, Any]]:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ExternalError("analysis evidence exceeds the response size limit")
        return [json.loads(line) for line in path.read_text().splitlines() if line]
