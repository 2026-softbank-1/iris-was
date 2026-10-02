"""Real PostgreSQL queue/API tests; source transport is controlled and models are unpaid."""

import asyncio
import copy
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.analyzer_client import LocalAnalyzerClient
from app.core.analysis_config import AnalysisSettings
from app.core.config import Settings, get_settings
from app.dependencies import get_analysis_service, get_current_user
from app.enums import AnalysisJobStatus, Builder
from app.main import app
from app.models.base import now_utc
from app.models.deployment_request import DeploymentRequest
from app.models.project import Project
from app.models.service import Service
from app.models.service_analysis import ServiceAnalysis
from app.models.user import GithubInstallation, User, UserGithubInstallation
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.service_analysis_repository import ServiceAnalysisRepository
from app.repositories.service_repository import ServiceRepository
from app.services.analysis_service import AnalysisService
from app.services.analysis_worker_service import AnalysisWorkerService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]
SHA = "a" * 40


class SourceClient:
    def __init__(self) -> None:
        self.sha = SHA
        self.calls = 0

    async def get_head_sha(self, repository_url: str, branch: str, installation_id: int) -> str:
        self.calls += 1
        return self.sha

    async def fetch_source(
        self, repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path:
        def write() -> Path:
            destination.mkdir()
            (destination / "package.json").write_text(
                '{"name":"app","scripts":{"start":"node index.js"},'
                '"dependencies":{"express":"5.1.0"}}'
            )
            (destination / "index.js").write_text(
                "const app = require('express')();\n"
                "app.get('/health', (req,res) => res.send('OK'));\n"
                "app.listen(process.env.PORT, '0.0.0.0');\n"
            )
            (destination / "Dockerfile").write_text(
                'FROM node:24\nWORKDIR /app\nCOPY . .\nCMD ["node", "index.js"]\n'
            )
            (destination / ".env").write_text("TOKEN=private-test-value")
            return destination

        return await asyncio.to_thread(write)


class Setup:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], user: User, service: Service
    ) -> None:
        self.factory = factory
        self.user = user
        self.service = service
        self.current_user = user
        self.source = SourceClient()
        self.settings = AnalysisSettings(_env_file=None)
        self.worker = AnalysisWorkerService(
            factory,
            self.source,
            LocalAnalyzerClient(budget_ledger=Path("/tmp/test-ledger")),
            self.settings,
        )

    def analysis_service(self, session: AsyncSession) -> AnalysisService:
        return AnalysisService(
            session,
            ServiceRepository(session),
            ServiceAnalysisRepository(session),
            GithubInstallationRepository(session),
            self.source,
            self.settings,
        )

    async def execute(self) -> ServiceAnalysis:
        row = await self.worker.claim_next_analysis()
        assert row is not None
        await self.worker.process_analysis(row)
        async with self.factory() as session:
            saved = await session.get(ServiceAnalysis, row.id)
            assert saved is not None
            return saved

    @property
    def url(self) -> str:
        return f"/api/v1/services/{self.service.id}/analysis"


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    identity = uuid.uuid4().int % (2**60)
    async with factory.begin() as session:
        user = User(github_id=identity, login="analysis-test")
        installation = GithubInstallation(
            installation_id=identity, account_login="analysis-test", account_type="User"
        )
        session.add_all([user, installation])
        await session.flush()
        project = Project(name="analysis-test", owner_id=user.id)
        session.add(project)
        await session.flush()
        service = Service(
            project_id=project.id,
            name="analysis-app",
            source_repository_url="https://github.com/team/app",
            github_installation_id=installation.id,
            source_branch="main",
            is_auto_deploy=False,
            builder=Builder.RAILPACK,
            port=9999,
            start_command="node existing.js",
        )
        session.add(service)
        session.add(UserGithubInstallation(user_id=user.id, github_installation_id=installation.id))
        await session.flush()
    state = Setup(factory, user, service)

    async def dependency() -> AsyncIterator[AnalysisService]:
        async with factory() as session:
            yield state.analysis_service(session)

    app.dependency_overrides[get_current_user] = lambda: state.current_user
    app.dependency_overrides[get_analysis_service] = dependency
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        async with factory.begin() as session:
            await session.execute(
                delete(ServiceAnalysis).where(ServiceAnalysis.service_id == service.id)
            )
            await session.execute(delete(Service).where(Service.id == service.id))
            await session.execute(delete(Project).where(Project.id == project.id))
            await session.execute(
                delete(UserGithubInstallation).where(UserGithubInstallation.user_id == user.id)
            )
            await session.execute(delete(User).where(User.id == user.id))
            await session.execute(
                delete(GithubInstallation).where(GithubInstallation.id == installation.id)
            )
        await engine.dispose()


@pytest.fixture
async def http() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_api_worker_actual_analyzer_result_review_and_preserved_settings(
    setup: Setup,
    http: AsyncClient,
) -> None:
    pytest.importorskip("iris_analyzer")
    missing = await http.get(setup.url)
    assert missing.status_code == 404 and missing.json()["code"] == "ANALYSIS_NOT_FOUND"
    created = await http.post(setup.url, json={"mode": "static"})
    assert created.status_code == 202
    analysis_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "QUEUED"
    assert created.json()["data"]["sourceSha"] == SHA
    row = await setup.execute()
    assert row.status == AnalysisJobStatus.SUCCEEDED
    response = await http.get(setup.url)
    data = response.json()["data"]
    assert data["id"] == analysis_id and data["deploymentAuthorized"] is False
    assert data["verificationReport"]["resultDigest"] == data["resultDigest"]
    candidate = data["analysisResult"]["services"][0]
    unknown_fields = [
        field
        for field in candidate.values()
        if isinstance(field, dict) and field.get("status") == "unknown"
    ]
    assert unknown_fields and all(
        "value" in field and field["value"] is None for field in unknown_fields
    )
    assert "private-test-value" not in response.text
    assert data["deploymentDossier"]["execution"]["deploymentAuthorized"] is False
    async with setup.factory() as session:
        service = await session.get(Service, setup.service.id)
        assert service is not None and service.port == 9999 and service.builder == Builder.RAILPACK
        assert service.start_command == "node existing.js"
    answer: dict[str, Any] = {
        "analysisId": analysis_id,
        "serviceCandidateId": candidate["serviceId"],
        "builder": "dockerfile",
        "dockerfilePath": "Dockerfile",
        "port": 8080,
    }
    confirmed = await http.post(setup.url + "/answers", json=answer)
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["confirmedAt"]
    assert confirmed.json()["data"]["reviewRequired"] is True  # Source gaps remain visible.
    async with setup.factory() as session:
        service = await session.get(Service, setup.service.id)
        assert (
            service is not None and service.port == 8080 and service.builder == Builder.DOCKERFILE
        )
        assert service.start_command == "node existing.js"  # Omitted field preserves user setting.
        assert service.analysis_plan["resultDigest"] == data["resultDigest"]
        assert await session.scalar(select(func.count()).select_from(DeploymentRequest)) == 0
    assert (await http.post(setup.url + "/answers", json=answer)).status_code == 200
    answer["port"] = 9090
    assert (await http.post(setup.url + "/answers", json=answer)).status_code == 409


async def test_unconfigured_model_and_owner_mismatch_never_read_source(
    setup: Setup,
    http: AsyncClient,
) -> None:
    response = await http.post(setup.url, json={"mode": "opencode"})
    assert response.status_code == 503 and response.json()["code"] == "MODEL_NOT_CONFIGURED"
    assert setup.source.calls == 0

    setup.current_user = User(id=setup.user.id + 1000, github_id=0, login="foreign")
    response = await http.post(setup.url, json={"mode": "static"})
    assert response.status_code == 404 and response.json()["code"] == "SERVICE_NOT_FOUND"
    assert setup.source.calls == 0


async def test_analysis_requires_real_session_authentication(
    setup: Setup, http: AsyncClient
) -> None:
    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=os.environ["TEST_DATABASE_URL"],
        session_secret="test-session-key",
        _env_file=None,
    )
    response = await http.post(setup.url, json={"mode": "static"})
    assert response.status_code == 401 and response.json()["code"] == "UNAUTHORIZED"
    assert response.headers.get("X-Request-ID")
    assert setup.source.calls == 0


async def test_concurrent_admission_creates_only_one_active_analysis(
    setup: Setup,
    http: AsyncClient,
) -> None:
    responses = await asyncio.gather(
        http.post(setup.url, json={"mode": "static"}),
        http.post(setup.url, json={"mode": "static"}),
    )
    assert sorted(response.status_code for response in responses) == [202, 409]
    async with setup.factory() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(ServiceAnalysis).where(
                            ServiceAnalysis.service_id == setup.service.id
                        )
                    )
                ).all()
            )
            == 1
        )


async def test_cancelled_analysis_cannot_be_claimed_or_completed(
    setup: Setup, http: AsyncClient
) -> None:
    response = await http.post(setup.url, json={"mode": "static"})
    analysis_id = response.json()["data"]["id"]
    claimed = await setup.worker.claim_next_analysis()
    assert claimed is not None and claimed.lease_token is not None
    cancel = await http.post(setup.url + "/cancel", json={"analysisId": analysis_id})
    assert cancel.status_code == 200 and cancel.json()["data"]["status"] == "CANCELLED"
    assert (
        await http.post(setup.url + "/cancel", json={"analysisId": analysis_id})
    ).status_code == 200
    await setup.worker._complete(claimed, claimed.lease_token, {})
    await setup.worker._fail(claimed.id, claimed.lease_token, "LATE_ERROR")
    assert await setup.worker.claim_next_analysis() is None
    assert (await http.get(setup.url)).json()["data"]["status"] == "CANCELLED"


async def test_queue_claim_competition_and_expired_lease_fences_old_owner(
    setup: Setup,
    http: AsyncClient,
) -> None:
    await http.post(setup.url, json={"mode": "static"})
    results = await asyncio.gather(
        setup.worker.claim_next_analysis(), setup.worker.claim_next_analysis()
    )
    owners = [row for row in results if row is not None]
    assert len(owners) == 1
    old = owners[0]
    assert old.lease_token is not None
    async with setup.factory.begin() as session:
        await session.execute(
            update(ServiceAnalysis)
            .where(ServiceAnalysis.id == old.id)
            .values(locked_until=now_utc() - timedelta(seconds=1))
        )
    replacement = await setup.worker.claim_next_analysis()
    assert replacement is not None and replacement.lease_token != old.lease_token
    await setup.worker._complete(old, old.lease_token, {})
    await setup.worker._fail(old.id, old.lease_token, "OLD_OWNER")
    async with setup.factory.begin() as session:
        repository = ServiceAnalysisRepository(session)
        assert not await repository.renew_lease(old.id, old.lease_token, 60)
        current = await repository.find_running(replacement.id, replacement.lease_token)
        assert current is not None and current.attempts == 2 and current.error_code is None


async def test_worker_does_not_use_changed_model_configuration(
    setup: Setup, http: AsyncClient
) -> None:
    setup.settings.provider = "hive-ai"
    setup.settings.model = "selected-model"
    setup.settings.server_url = "http://localhost:9999"
    assert (await http.post(setup.url, json={"mode": "opencode"})).status_code == 202
    setup.settings.model = "different-model"
    row = await setup.execute()
    assert (
        row.status == AnalysisJobStatus.FAILED and row.error_code == "MODEL_CONFIGURATION_CHANGED"
    )
    assert setup.source.calls == 1  # Admission only; no download/model execution.


async def test_stale_head_is_rejected_before_execution(setup: Setup, http: AsyncClient) -> None:
    await http.post(setup.url, json={"mode": "static"})
    setup.source.sha = "b" * 40
    row = await setup.execute()
    assert row.status == AnalysisJobStatus.FAILED and row.error_code == "ANALYSIS_STALE"


async def test_answers_reject_new_head_and_missing_candidates(
    setup: Setup, http: AsyncClient
) -> None:
    pytest.importorskip("iris_analyzer")
    await http.post(setup.url, json={"mode": "static"})
    row = await setup.execute()
    assert row.analysis_result is not None
    answers = {"analysisId": row.id, "serviceCandidateId": "foreign", "builder": "railpack"}
    response = await http.post(setup.url + "/answers", json=answers)
    assert response.status_code == 422 and response.json()["code"] == "ANALYSIS_CANDIDATE_INVALID"
    answers["serviceCandidateId"] = row.analysis_result["services"][0]["serviceId"]
    setup.source.sha = "b" * 40
    response = await http.post(setup.url + "/answers", json=answers)
    assert response.status_code == 409 and response.json()["code"] == "ANALYSIS_STALE"


async def test_worker_cancellation_requeues_only_its_current_lease(
    setup: Setup,
    http: AsyncClient,
) -> None:
    started = asyncio.Event()

    async def wait(*args: object, **kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    setup.worker._analyzer_client.analyze = wait
    await http.post(setup.url, json={"mode": "static"})
    claimed = await setup.worker.claim_next_analysis()
    assert claimed is not None
    task = asyncio.create_task(setup.worker.process_analysis(claimed))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    await task
    assert (await http.get(setup.url)).json()["data"]["status"] == "QUEUED"


async def test_worker_timeout_records_failure_without_result(
    setup: Setup, http: AsyncClient
) -> None:
    async def timed_out(*args: object, **kwargs: object) -> dict[str, Any]:
        raise TimeoutError

    setup.worker._analyzer_client.analyze = timed_out
    await http.post(setup.url, json={"mode": "static"})
    row = await setup.execute()
    assert row.status == AnalysisJobStatus.FAILED and row.error_code == "ANALYSIS_TIMED_OUT"
    assert row.analysis_result is None


async def test_worker_does_not_persist_tampered_result(setup: Setup, http: AsyncClient) -> None:
    pytest.importorskip("iris_analyzer")
    original = setup.worker._analyzer_client.analyze

    async def tamper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        output = copy.deepcopy(await original(*args, **kwargs))
        output["analysisResult"]["status"] = "unsupported"
        return output

    setup.worker._analyzer_client.analyze = tamper
    await http.post(setup.url, json={"mode": "static"})
    row = await setup.execute()
    assert row.status == AnalysisJobStatus.FAILED and row.error_code == "INVALID_ANALYZER_RESULT"
    assert row.analysis_result is None and row.verification_report is None


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "invalid"},
        {"mode": "static", "sourceRoot": "/etc"},
        {"mode": "static", "apiKey": "secret"},
    ],
)
async def test_api_rejects_caller_paths_and_model_secrets(
    setup: Setup,
    http: AsyncClient,
    body: dict[str, str],
) -> None:
    response = await http.post(setup.url, json=body)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_ERROR"
    assert "secret" not in response.text
    assert setup.source.calls == 0
