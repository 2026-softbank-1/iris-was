"""Owned API, actual PostgreSQL leases and model-free v2 diagnostic execution."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.failure_log_client import FailureLogClient
from app.core.diagnosis_config import DiagnosisSettings
from app.dependencies import get_current_user, get_diagnosis_service
from app.enums import (
    DeploymentStatus,
    DeploymentTrigger,
    DiagnosisJobStatus,
    Environment,
    FailureCode,
    JobKind,
    JobStatus,
)
from app.main import app
from app.models.base import now_utc
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job
from app.models.project import Project
from app.models.service import Service
from app.models.user import GithubInstallation, User
from app.services.diagnosis_service import DiagnosisService, enqueue_deployment_diagnosis
from app.services.diagnosis_worker_service import DiagnosisWorkerService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


class ModelFreeDiagnosis:
    def __init__(self):
        self.calls = 0
        self.mode = "ok"
        self.requests = []

    async def diagnose(self, request: dict[str, Any]):
        from ai_error_check_agent.contracts import DiagnosisRequest
        from ai_error_check_agent.direct_api import DirectModelProfile
        from ai_error_check_agent.runtime import ModelResponse
        from ai_error_check_agent.service import diagnose

        self.calls += 1
        self.requests.append(request)
        if self.mode == "timeout":
            raise TimeoutError
        if self.mode == "fail":
            raise RuntimeError("private error text")
        if self.mode == "sleep":
            await asyncio.sleep(30)
        text = json.dumps(
            {
                "analysis_status": "insufficient_evidence",
                "summary": (
                    "Missing configuration was recorded; the supplied message "
                    "does not identify where configuration was lost."
                ),
                "observations": [
                    {
                        "id": "O1",
                        "kind": "failure",
                        "text": "Application startup failed.",
                        "evidence_ids": ["EV000001"],
                    }
                ],
                "hypotheses": [],
                "next_checks": [
                    {
                        "id": "C1",
                        "target": "Deployment configuration",
                        "method": "Check configured keys without exposing values.",
                        "purpose": "Determine which configuration keys were delivered.",
                        "hypothesis_ids": [],
                    }
                ],
                "missing_information": [
                    {
                        "requested_data": "Configuration key names and propagation status",
                        "reason": "Only an operation message is available.",
                    }
                ],
                "limitations": ["No runtime pod logs were supplied."],
                "remediation": {
                    "status": "needs_more_evidence",
                    "reason": "The operation message alone does not isolate a cause.",
                    "plans": [],
                },
            }
        )

        class Runtime:
            profile = DirectModelProfile(
                profile_id=request["model_profile_id"], model_id="example-model"
            )
            message_submissions = 1
            provider_call_count = 0
            runtime_version = "model-free-test.v1"
            cleanup_status = "not_needed"
            abort_confirmed = None
            reusable = True

            async def run(self, *args):
                return ModelResponse(text, {})

        output = await diagnose(
            DiagnosisRequest.model_validate_json(json.dumps(request)), Runtime()
        )
        if self.mode == "forge":
            output["scope"]["deployment_id"] = "foreign-deployment"
        return output


class Setup:
    def __init__(self, factory, user, service, deployment):
        self.factory = factory
        self.user = user
        self.current_user = user
        self.service = service
        self.deployment = deployment
        self.settings = DiagnosisSettings(
            _env_file=None,
            model="example-model",
            api_key=SecretStr("fake-test-key"),
            input_usd_per_million=1,
            output_usd_per_million=1,
        )
        self.client = ModelFreeDiagnosis()
        self.worker = DiagnosisWorkerService(
            factory, FailureLogClient(self.settings), self.client, self.settings
        )

    @property
    def url(self):
        return f"/api/v1/services/{self.service.id}/deployments/{self.deployment.id}/diagnosis"

    async def execute(self):
        row = await self.worker.claim_next_diagnosis()
        assert row is not None
        await self.worker.process_diagnosis(row)
        async with self.factory() as session:
            return await session.get(DeploymentDiagnosis, row.id)


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    identity = uuid.uuid4().int % (2**60)
    async with factory.begin() as session:
        user = User(github_id=identity, login="diagnosis-test")
        installation = GithubInstallation(
            installation_id=identity, account_login="diagnosis-test", account_type="User"
        )
        session.add_all([user, installation])
        await session.flush()
        project = Project(name="diagnosis-test", owner_id=user.id)
        session.add(project)
        await session.flush()
        service = Service(
            project_id=project.id,
            name="diagnosis-app",
            source_repository_url="https://github.com/team/app",
            github_installation_id=installation.id,
            source_branch="main",
            is_auto_deploy=False,
        )
        session.add(service)
        await session.flush()
        deployment = DeploymentRequest(
            service_id=service.id,
            environment=Environment.PROD,
            source_sha="a" * 40,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key=str(uuid.uuid4()),
            requested_by=user.id,
            status=DeploymentStatus.FAILED,
            failure_code=FailureCode.DEPLOY_FAILED,
            variables_snapshot={
                "PASSWORD": "bare-private-value",
                "bindings": [{"key": "CUSTOM_KEY", "value": "pipeline-private-value"}],
            },
        )
        session.add(deployment)
        await session.flush()
        job = Job(
            deployment_request_id=deployment.id,
            kind=JobKind.DEPLOY,
            status=JobStatus.FAILED,
            payload={
                "failureLog": {
                    "sourceId": "argo:iris-app",
                    "stage": "deploy",
                    "text": (
                        "ERROR Application startup failed: "
                        "bare-private-value pipeline-private-value"
                    ),
                    "sourceLineStart": 1,
                    "isComplete": False,
                    "artifactRef": "b" * 40,
                }
            },
            attempts=1,
        )
        session.add(job)
        await session.flush()
    state = Setup(factory, user, service, deployment)

    async def dependency():
        async with factory() as session:
            yield DiagnosisService(session, state.settings)

    app.dependency_overrides[get_current_user] = lambda: state.current_user
    app.dependency_overrides[get_diagnosis_service] = dependency
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        async with factory.begin() as session:
            await session.execute(
                delete(DeploymentDiagnosis).where(DeploymentDiagnosis.service_id == service.id)
            )
            await session.execute(delete(Job).where(Job.deployment_request_id == deployment.id))
            await session.execute(
                delete(DeploymentRequest).where(DeploymentRequest.id == deployment.id)
            )
            await session.execute(delete(Service).where(Service.id == service.id))
            await session.execute(delete(Project).where(Project.id == project.id))
            await session.execute(delete(User).where(User.id == user.id))
            await session.execute(
                delete(GithubInstallation).where(GithubInstallation.id == installation.id)
            )
        await engine.dispose()


@pytest.fixture
async def http() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_owned_api_idempotent_actual_v2_and_deployment_status_preserved(setup, http):
    missing = await http.get(setup.url)
    assert missing.status_code == 404 and missing.json()["code"] == "DIAGNOSIS_NOT_FOUND"
    created = await http.post(setup.url)
    repeated = await http.post(setup.url)
    assert created.status_code == repeated.status_code == 202
    assert created.json()["data"]["id"] == repeated.json()["data"]["id"]
    row = await setup.execute()
    assert row.status == DiagnosisJobStatus.SUCCEEDED
    assert setup.client.calls == 1
    assert "bare-private-value" not in json.dumps(row.source_logs)
    assert "pipeline-private-value" not in json.dumps(row.source_logs)
    response = await http.get(setup.url)
    result = response.json()["data"]["result"]
    assert result["schema_version"] == "diagnosis-result.v2"
    assert result["analysis"]["analysis_status"] == "insufficient_evidence"
    assert result["analysis"]["remediation"]["status"] == "needs_more_evidence"
    assert result["remediation_execution"] == "not_executed"
    assert result["evidence"][0]["text"].endswith("[REDACTED]")
    assert result["log_sources"][0]["artifact_ref"] == "b" * 40
    assert any("runtime pod logs unavailable" in item for item in result["input_limitations"])
    async with setup.factory() as session:
        deployment = await session.get(DeploymentRequest, setup.deployment.id)
        assert deployment.status == DeploymentStatus.FAILED
        assert deployment.failure_code == FailureCode.DEPLOY_FAILED
        assert deployment.variables_snapshot["PASSWORD"] == "bare-private-value"
    setup.current_user = User(id=setup.user.id + 999999, github_id=1, login="foreign")
    assert (await http.get(setup.url)).status_code == 404
    assert (await http.post(setup.url)).status_code == 404


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("fail", DiagnosisJobStatus.FAILED),
        ("timeout", DiagnosisJobStatus.TIMED_OUT),
        ("forge", DiagnosisJobStatus.FAILED),
    ],
)
async def test_diagnosis_failure_never_changes_deployment(setup, http, mode, expected):
    setup.client.mode = mode
    assert (await http.post(setup.url)).status_code == 202
    row = await setup.execute()
    assert row.status == expected and row.result is None
    async with setup.factory() as session:
        deployment = await session.get(DeploymentRequest, setup.deployment.id)
        assert deployment.status == DeploymentStatus.FAILED


async def test_automatic_unconfigured_job_is_visible_and_no_paid_call(setup, http):
    setup.settings.model = None
    async with setup.factory.begin() as session:
        deployment = await session.get(DeploymentRequest, setup.deployment.id)
        first = await enqueue_deployment_diagnosis(session, deployment, settings=setup.settings)
        second = await enqueue_deployment_diagnosis(session, deployment, settings=setup.settings)
        assert first.id == second.id
    row = await setup.execute()
    assert row.status == DiagnosisJobStatus.FAILED and row.error_code == "MODEL_NOT_CONFIGURED"
    assert setup.client.calls == 0
    assert (await http.get(setup.url)).json()["data"]["errorCode"] == "MODEL_NOT_CONFIGURED"


async def test_missing_logs_do_not_call_model_or_manufacture_analysis(setup, http):
    async with setup.factory.begin() as session:
        await session.execute(
            update(Job).where(Job.deployment_request_id == setup.deployment.id).values(payload={})
        )
    assert (await http.post(setup.url)).status_code == 202
    row = await setup.execute()
    assert row.error_code == "DIAGNOSIS_LOGS_UNAVAILABLE" and row.result is None
    assert setup.client.calls == 0


async def test_expired_model_attempt_recovery_never_duplicates_inference(setup, http):
    assert (await http.post(setup.url)).status_code == 202
    row = await setup.worker.claim_next_diagnosis()
    async with setup.factory.begin() as session:
        await session.execute(
            update(DeploymentDiagnosis)
            .where(DeploymentDiagnosis.id == row.id)
            .values(stage="model_started", locked_until=now_utc() - timedelta(seconds=1))
        )
    reclaimed = await setup.worker.claim_next_diagnosis()
    assert reclaimed.lease_token != row.lease_token
    await setup.worker.process_diagnosis(reclaimed)
    async with setup.factory() as session:
        saved = await session.get(DeploymentDiagnosis, row.id)
        assert saved.error_code == "DIAGNOSIS_RECOVERY_UNCERTAIN"
    assert setup.client.calls == 0


async def test_only_failed_deployments_are_admitted_and_manual_unconfigured_is_503(setup, http):
    setup.settings.model = None
    assert (await http.post(setup.url)).status_code == 503
    async with setup.factory.begin() as session:
        await session.execute(
            update(DeploymentRequest)
            .where(DeploymentRequest.id == setup.deployment.id)
            .values(status=DeploymentStatus.SUCCEEDED)
        )
    assert (await http.post(setup.url)).status_code == 409
