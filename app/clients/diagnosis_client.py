"""Use the pinned diagnosis library without running any proposed remediation."""

import asyncio
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from app.core.async_io import run_sync
from app.core.diagnosis_config import DiagnosisSettings
from app.core.exceptions import AppError, NotConfiguredError

_JSON = TypeAdapter(dict[str, JsonValue])


class DiagnosisExecutionError(AppError):
    code = "DIAGNOSIS_EXECUTION_FAILED"
    status_code = 502


class DiagnosisClient(Protocol):
    async def diagnose(self, request: dict[str, Any]) -> dict[str, JsonValue]: ...


def _reserve(settings: DiagnosisSettings, request: dict[str, Any]) -> str:
    """Use the same locked ledger format as iris_analyzer.budget.BudgetedRunner.

    Do not refund reservations: timeouts can leave a remote generation running,
    and direct responses do not report an authoritative billed dollar amount.
    """
    input_rate = settings.input_usd_per_million
    output_rate = settings.output_usd_per_million
    if input_rate is None or output_rate is None:
        raise NotConfiguredError("diagnosis token pricing is not configured")
    reserve = (
        settings.max_prompt_bytes * input_rate + settings.max_output_tokens * output_rate
    ) / 1_000_000
    path: Path = settings.budget_ledger
    path.parent.mkdir(parents=True, exist_ok=True)
    identifier = uuid4().hex
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        try:
            raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("ledger too large")
            state: dict[str, Any] = (
                json.loads(raw) if raw else {"schemaVersion": "1", "entries": []}
            )
            if state.get("schemaVersion") != "1" or not isinstance(state.get("entries"), list):
                raise ValueError("invalid ledger")
            committed = 0.0
            for entry in state["entries"]:
                amount = entry["estimatedOrReservedUsd"]
                if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                    raise ValueError("invalid reservation")
                committed += amount
        except (ValueError, AttributeError, TypeError, KeyError):
            raise DiagnosisExecutionError("invalid shared model budget ledger") from None
        if committed + reserve > settings.max_cost_usd:
            raise DiagnosisExecutionError(
                "shared model budget exhausted", reason="MODEL_BUDGET_EXCEEDED"
            )
        state["entries"].append(
            {
                "id": identifier,
                "state": "reserved",
                "contextHash": request["deployment_id"] + ":" + request["attempt_id"],
                "provider": settings.provider,
                "model": settings.model,
                "reservedUsd": reserve,
                "estimatedOrReservedUsd": reserve,
                "actualCostUsd": None,
                "purpose": "deployment_diagnosis",
            }
        )
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(state, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return identifier


class LocalDiagnosisClient:
    def __init__(self, settings: DiagnosisSettings) -> None:
        self._settings = settings
        self._slots = asyncio.Semaphore(1)

    async def diagnose(self, request: dict[str, Any]) -> dict[str, JsonValue]:
        try:
            from ai_error_check_agent.contracts import DiagnosisRequest
            from ai_error_check_agent.direct_api import DirectModelProfile, OpenAIResponsesRuntime
            from ai_error_check_agent.errors import DiagnosisError
            from ai_error_check_agent.preprocessing import prepare
            from ai_error_check_agent.service import diagnose
        except ImportError:
            raise NotConfiguredError("diagnosis worker package is unavailable") from None
        selection = self._settings.model_selection()
        if selection is None or self._settings.api_key is None:
            raise NotConfiguredError("diagnosis model is not configured")
        parsed = DiagnosisRequest.model_validate_json(json.dumps(request))
        # Reject oversize input before reserving money or sending an HTTP request.
        prepare(parsed, self._settings.max_evidence_bytes)
        profile = DirectModelProfile(
            profile_id=self._settings.profile_id,
            model_id=self._settings.model,
            timeout_seconds=float(self._settings.timeout_seconds),
            max_output_tokens=self._settings.max_output_tokens,
            reasoning_effort=self._settings.reasoning_effort,
            max_evidence_bytes=self._settings.max_evidence_bytes,
            max_prompt_bytes=self._settings.max_prompt_bytes,
        )
        async with self._slots:
            reservation_id = await run_sync(lambda: _reserve(self._settings, request))
            runtime = OpenAIResponsesRuntime(profile, self._settings.api_key)
            try:
                output = await diagnose(parsed, runtime)
                output["execution"]["budget_reservation_id"] = reservation_id
                if (
                    output.get("schema_version") != "diagnosis-result.v2"
                    or output.get("remediation_execution") != "not_executed"
                    or output.get("scope")
                    != {
                        key: request[key]
                        for key in ("tenant_id", "project_id", "deployment_id", "attempt_id")
                    }
                    or output.get("deployment_context") != request["context"]
                ):
                    raise DiagnosisExecutionError("diagnosis output changed fixed provenance")
                return _JSON.validate_json(json.dumps(output, allow_nan=False))
            except DiagnosisError as error:
                raise DiagnosisExecutionError(
                    "diagnosis input or result is invalid", reason=error.code
                ) from None
            finally:
                await runtime.close()
