"""Unpaid diagnosis adapter contracts and the shared monetary guard."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.clients.diagnosis_client import DiagnosisExecutionError, LocalDiagnosisClient, _reserve
from app.core.diagnosis_config import DiagnosisSettings
from app.core.exceptions import NotConfiguredError


def settings(path: Path, **overrides):
    return DiagnosisSettings(
        _env_file=None,
        model="example-model",
        api_key=SecretStr("fake-test-key"),
        input_usd_per_million=1,
        output_usd_per_million=1,
        budget_ledger=path,
        **overrides,
    )


def request():
    return {
        "schema_version": "diagnosis-request.v1",
        "tenant_id": "user-1",
        "project_id": "project-1",
        "deployment_id": "deployment-1",
        "attempt_id": "deployment-1",
        "trigger": "deployment_failed",
        "context": {"reported_stage": "build", "deployment_status": "failed", "exit_code": None},
        "logs": [
            {
                "chunk_id": "log-1",
                "source_id": "cloudwatch-1",
                "stage": "build",
                "stream": "combined",
                "source_line_start": None,
                "captured_at": "2026-10-02T03:42:00+00:00",
                "is_complete": False,
                "text": "Build failed: dependency not found",
            }
        ],
        "model_profile_id": "iris-deployment-diagnosis",
        "model_settings_version": "settings-1",
        "previous_diagnosis_id": None,
    }


def test_shared_ledger_reserves_before_inference_and_keeps_analysis_spending(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps({"schemaVersion": "1", "entries": [{"estimatedOrReservedUsd": 0.2}]})
    )
    configuration = settings(ledger)
    identifier = _reserve(configuration, request())
    state = json.loads(ledger.read_text())
    assert state["entries"][0]["estimatedOrReservedUsd"] == 0.2
    assert state["entries"][1]["id"] == identifier
    assert state["entries"][1]["estimatedOrReservedUsd"] == pytest.approx(0.036864)


def test_budget_limit_and_invalid_ledger_refuse_inference(tmp_path):
    configuration = settings(tmp_path / "ledger.json", max_cost_usd=0.01)
    with pytest.raises(DiagnosisExecutionError) as error:
        _reserve(configuration, request())
    assert error.value.fields["reason"] == "MODEL_BUDGET_EXCEEDED"
    configuration.budget_ledger.write_text(
        '{"schemaVersion":"1","entries":[{"estimatedOrReservedUsd":-1}]}'
    )
    with pytest.raises(DiagnosisExecutionError):
        _reserve(configuration, request())


async def test_adapter_reuses_v2_library_preserves_datetime_and_never_executes_remediation(
    tmp_path, monkeypatch
):
    from ai_error_check_agent import direct_api, service

    seen = {}

    class Runtime:
        def __init__(self, profile, key):
            self.profile = profile
            seen["runtime_created"] = True
            assert json.loads((tmp_path / "ledger.json").read_text())["entries"]

        async def close(self):
            seen["closed"] = True

    async def diagnose(parsed, runtime):
        seen["captured_at"] = parsed.logs[0].captured_at
        return {
            "schema_version": "diagnosis-result.v2",
            "remediation_execution": "not_executed",
            "scope": {
                key: getattr(parsed, key)
                for key in ("tenant_id", "project_id", "deployment_id", "attempt_id")
            },
            "deployment_context": parsed.context.model_dump(),
            "job_status": "succeeded",
            "analysis": {
                "analysis_status": "insufficient_evidence",
                "remediation": {"status": "needs_more_evidence", "plans": []},
            },
            "execution": {"tokens": None},
        }

    monkeypatch.setattr(direct_api, "OpenAIResponsesRuntime", Runtime)
    monkeypatch.setattr(service, "diagnose", diagnose)
    output = await LocalDiagnosisClient(settings(tmp_path / "ledger.json")).diagnose(request())
    assert output["schema_version"] == "diagnosis-result.v2"
    assert output["remediation_execution"] == "not_executed"
    assert seen["captured_at"].tzinfo is not None
    assert seen["closed"]


async def test_unconfigured_adapter_and_oversize_input_never_dispatch(tmp_path):
    client = LocalDiagnosisClient(DiagnosisSettings(_env_file=None))
    with pytest.raises(NotConfiguredError):
        await client.diagnose(request())
    from ai_error_check_agent.errors import DiagnosisError

    data = request()
    data["logs"][0]["text"] = "Build failed\n" * 1000
    with pytest.raises(DiagnosisError):
        await LocalDiagnosisClient(settings(tmp_path / "ledger.json")).diagnose(data)
    assert not (tmp_path / "ledger.json").exists()
