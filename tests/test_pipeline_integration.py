"""Actual analyzer and PostgreSQL coordination; no paid inference or cloud mutations."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update

from app.clients.analyzer_client import LocalAnalyzerClient
from app.dependencies import get_pipeline_service
from app.enums import PipelineStatus
from app.main import app
from app.models.build import Build
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.pipeline_run import PipelineRun
from app.models.release import Release
from app.models.service import Service
from app.models.target import ServiceTarget, Target
from app.services.pipeline_contract import digest
from app.services.pipeline_service import PipelineService, enqueue_push_pipeline
from app.services.pipeline_worker_service import PipelineWorkerService
from tests.test_analysis_integration import Setup, setup  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def pipeline_setup(setup: Setup) -> AsyncIterator[tuple[Setup, PipelineWorkerService]]:  # noqa: F811
    async with setup.factory.begin() as session:
        aws = await session.scalar(select(Target).where(Target.name == "aws"))
        assert aws is not None
        session.add(ServiceTarget(service_id=setup.service.id, target_id=aws.id))

    async def dependency() -> AsyncIterator[PipelineService]:
        async with setup.factory() as session:
            yield PipelineService(session, setup.source, setup.settings)

    app.dependency_overrides[get_pipeline_service] = dependency
    worker = PipelineWorkerService(
        setup.factory,
        setup.source,
        LocalAnalyzerClient(budget_ledger=Path("/tmp/pipeline-integration-ledger")),
        setup.settings,
    )
    try:
        yield setup, worker
    finally:
        async with setup.factory.begin() as session:
            await session.execute(
                delete(PipelineRun).where(PipelineRun.service_id == setup.service.id)
            )
            ids = select(DeploymentRequest.id).where(
                DeploymentRequest.service_id == setup.service.id
            )
            await session.execute(
                delete(DeploymentDiagnosis).where(DeploymentDiagnosis.deployment_id.in_(ids))
            )
            await session.execute(delete(Release).where(Release.deployment_request_id.in_(ids)))
            await session.execute(delete(Build).where(Build.deployment_request_id.in_(ids)))
            await session.execute(delete(Job).where(Job.deployment_request_id.in_(ids)))
            await session.execute(
                delete(DeploymentStatusHistory).where(
                    DeploymentStatusHistory.deployment_request_id.in_(ids)
                )
            )
            await session.execute(
                delete(DeploymentRequest).where(DeploymentRequest.service_id == setup.service.id)
            )
            await session.execute(
                delete(ServiceTarget).where(ServiceTarget.service_id == setup.service.id)
            )


async def test_single_click_automatically_analyzes_then_plans_then_enqueues_fixed_build(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
) -> None:
    state, worker = pipeline_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        response = await http.post(url, json={"mode": "static", "autoDeploy": True})
        assert response.status_code == 202, response.text
        assert response.json()["data"]["status"] == "QUEUED"
        await worker.tick()
        assert (await http.get(url)).json()["data"]["status"] == "ANALYZING"
        await state.execute()
        await worker.tick()
        status = (await http.get(url)).json()["data"]
        assert status["status"] == "BUILDING", status
        assert status["executionPlan"]["sourceSha"] == state.source.sha
        assert status["planDigest"] == digest(status["executionPlan"])
        assert status["planningReport"]["mode"] == "policy"
        direct = await http.post(
            f"/api/v1/services/{state.service.id}/deployments", json={"triggerType": "MANUAL"}
        )
        assert direct.status_code == 409 and direct.json()["code"] == "PIPELINE_REQUIRED"
        async with state.factory() as session:
            jobs = (await session.scalars(select(Job))).all()
            assert len(jobs) == 1
            config = jobs[0].payload["build_config"]
            assert config["port"] == 9999 and config["start_command"] == "node existing.js"
            assert config["analysis_plan_digest"] == status["planDigest"]
            assert jobs[0].payload["source_sha"] == state.source.sha


async def test_missing_information_waits_and_answer_resumes_planning_before_build(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
) -> None:
    state, worker = pipeline_setup
    async with state.factory.begin() as session:
        await session.execute(
            update(Service).where(Service.id == state.service.id).values(port=None)
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        run = (await http.post(url, json={"mode": "static"})).json()["data"]
        await worker.tick()
        await state.execute()
        await worker.tick()
        waiting = (await http.get(url)).json()["data"]
        assert waiting["status"] == "AWAITING_INPUT", waiting
        assert any(question["key"] == "port" for question in waiting["questions"])
        async with state.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Job)) == 0
        answered = await http.post(url + f"/{run['id']}/answers", json={"port": 8080})
        assert answered.status_code == 202, answered.text
        await worker.tick()
        completed = (await http.get(url)).json()["data"]
        assert completed["status"] == "BUILDING", completed
        assert completed["executionPlan"]["buildConfig"]["port"] == 8080


async def test_pipeline_idempotency_and_foreign_owner_boundaries(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
) -> None:
    state, _ = pipeline_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        first = await http.post(
            url, json={"mode": "static"}, headers={"Idempotency-Key": "one-click"}
        )
        second = await http.post(
            url, json={"mode": "static"}, headers={"Idempotency-Key": "one-click"}
        )
        assert first.json()["data"]["id"] == second.json()["data"]["id"]
        changed = await http.post(
            url,
            json={"mode": "static", "autoDeploy": False},
            headers={"Idempotency-Key": "one-click"},
        )
        assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
        state.current_user.id += 10000
        assert (await http.get(url)).status_code == 404
        state.current_user.id -= 10000


async def test_managed_push_always_enqueues_analysis_and_no_build(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
) -> None:
    state, _ = pipeline_setup
    async with state.factory.begin() as session:
        service = await session.get(Service, state.service.id)
        assert service is not None
        service.analysis_plan = {"pipelineManaged": True, "workflowMode": "static"}
        first = await enqueue_push_pipeline(session, service, "b" * 40, "delivery-1")
        second = await enqueue_push_pipeline(session, service, "b" * 40, "delivery-1")
        assert first is not None and first.status == PipelineStatus.QUEUED and second is None
        assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_pipeline_cancel_is_terminal_before_models_or_builds(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
) -> None:
    state, worker = pipeline_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        run = (await http.post(url, json={"mode": "static"})).json()["data"]
        cancelled = await http.post(url + f"/{run['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "CANCELLED"
        assert await worker.tick() is False


@pytest.mark.parametrize("changed", ["config", "targets"])
async def test_changed_service_inputs_never_admit_an_old_build_plan(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
    changed: str,
) -> None:
    state, worker = pipeline_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        assert (await http.post(url, json={"mode": "static"})).status_code == 202
        await worker.tick()
        await state.execute()
        async with state.factory.begin() as session:
            if changed == "config":
                await session.execute(
                    update(Service).where(Service.id == state.service.id).values(port=8081)
                )
            else:
                await session.execute(
                    delete(ServiceTarget).where(ServiceTarget.service_id == state.service.id)
                )
        await worker.tick()
        failed = (await http.get(url)).json()["data"]
        assert failed["status"] == "FAILED" and failed["errorCode"] == "PIPELINE_STALE", failed
        async with state.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_actual_analyzer_readiness_errors_pause_before_build(
    pipeline_setup: tuple[Setup, PipelineWorkerService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, worker = pipeline_setup
    original_fetch = state.source.fetch_source

    async def incompatible_source(
        repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path:
        import asyncio
        import json

        source = await original_fetch(repository_url, source_sha, installation_id, destination)

        def modify() -> None:
            package = json.loads((source / "package.json").read_text())
            package["engines"] = {"node": ">=20"}
            (source / "package.json").write_text(json.dumps(package))
            (source / "Dockerfile").write_text(
                'FROM node:18\nWORKDIR /app\nCOPY . .\nCMD ["node", "index.js"]\n'
            )

        await asyncio.to_thread(modify)
        return source

    monkeypatch.setattr(state.source, "fetch_source", incompatible_source)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        url = f"/api/v1/services/{state.service.id}/pipelines"
        assert (await http.post(url, json={"mode": "static"})).status_code == 202
        await worker.tick()
        await state.execute()
        await worker.tick()
        waiting = (await http.get(url)).json()["data"]
        assert waiting["status"] == "AWAITING_INPUT", waiting
        assert any(
            q["key"] == "sourceReadiness.runtime.incompatible_major_constraint"
            and q["kind"] == "code_review"
            for q in waiting["questions"]
        )
        async with state.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Job)) == 0
