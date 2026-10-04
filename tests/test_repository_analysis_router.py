"""레포 구성 분석 API: 접수(202)·조회·apply(201, 멱등)와 서비스 생성의 analysisId(skip 경로).

분석 실행은 Build Worker 몫이라 여기서는 결과를 Repository 에 직접 기록해 흉내 낸다.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.clients.source_repository_client import BranchInfo, CommitInfo
from app.dependencies import (
    get_current_user,
    get_repository_analysis_service,
    get_service_registry_service,
)
from app.enums import AnalysisErrorCode, AnalysisGateComplexity, AnalysisGateDecision
from app.main import app
from app.models.project import Project
from app.models.repository_analysis import RepositoryAnalysis
from app.models.user import User
from app.services.repository_analysis_service import RepositoryAnalysisService
from app.services.service_registry_service import ServiceRegistryService
from app.services.source_repository_service import SourceRepositoryService
from tests.fakes import (
    FakeGithubInstallationRepository,
    FakeSession,
    FakeSourceRepositoryClient,
    make_installation,
    make_repository,
)
from tests.fakes_project import (
    FakeProjectRepository,
    FakeServiceRepository,
    FakeTargetRepository,
    FakeTeardownService,
)
from tests.fakes_repository_analysis import (
    FakeManualDeploymentService,
    FakeRepositoryAnalysisRepository,
)
from tests.fakes_webhook import FakeDeploymentRequestRepository

SHA = "c" * 40
REPOSITORY_URL = "https://github.com/iris-org/shop"


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


def complex_result(**overrides: Any) -> dict[str, Any]:
    return {
        "schemaVersion": "iris.analysis-gate.v1",
        "sourceSha": SHA,
        "rootDirectory": ".",
        "decision": "analyze",
        "complexity": "complex",
        "reasons": [{"code": "compose_multi_build", "message": "m", "paths": ["compose.yaml"]}],
        "signals": {"composeBuildServices": ["web", "api", "worker"]},
        "simpleBuild": None,
        "units": [
            {
                "id": "web",
                "name": "web",
                "rootDirectory": "web",
                "builder": "dockerfile",
                "dockerfilePath": "Dockerfile",
                "port": 3000,
                "startCommand": None,
                "buildCommand": None,
                "role": "web",
                "public": True,
                "env": [],
                "dependsOn": ["api"],
                "evidence": [{"path": "compose.yaml", "line": 3}],
            },
            {
                "id": "api",
                "name": "Shop API",
                "rootDirectory": "services/api",
                "builder": "dockerfile",
                "dockerfilePath": "docker/Dockerfile.prod",
                "port": 8000,
                "startCommand": "node dist/server.js",
                "buildCommand": None,
                "role": "api",
                "public": True,
                "env": [{"key": "DATABASE_URL", "stage": "runtime", "required": True}],
                "dependsOn": ["postgres"],
                "evidence": [],
            },
            {
                "id": "worker",
                "name": "worker",
                "rootDirectory": "worker",
                "builder": "railpack",
                "dockerfilePath": None,
                "port": None,
                "startCommand": "python -m worker",
                "buildCommand": "pip install .",
                "role": "worker",
                "public": False,
                "env": [],
                "dependsOn": ["redis"],
                "evidence": [],
            },
        ],
        "dependencies": [{"id": "postgres", "engine": "postgres", "image": "postgres:16"}],
        "questions": [{"code": "port_unknown", "unitId": "worker", "message": "?"}],
        "analysis": {"engine": "static", "durationMs": 10, "modelCalls": 0},
        "executionAuthorized": False,
        **overrides,
    }


def simple_result() -> dict[str, Any]:
    return complex_result(
        decision="skip",
        complexity="simple",
        reasons=[{"code": "single_dockerfile", "message": "m", "paths": ["Dockerfile.web"]}],
        simpleBuild={"builder": "dockerfile", "dockerfilePath": "Dockerfile.web"},
        units=[],
        dependencies=[],
        questions=[],
    )


class Setup:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.targets = FakeTargetRepository()
        self.installations = FakeGithubInstallationRepository()
        self.deployments = FakeDeploymentRequestRepository()
        self.analyses = FakeRepositoryAnalysisRepository(self.projects)
        self.manual = FakeManualDeploymentService()
        self.github = FakeSourceRepositoryClient(
            {22: [make_repository("iris-org/shop"), make_repository("iris-org/other")]}
        )
        for name in ("iris-org/shop", "iris-org/other"):
            self.github.branches[name] = [BranchInfo("main", True), BranchInfo("dev", False)]
            self.github.heads[(name, "main")] = CommitInfo(SHA, "init")
            self.github.heads[(name, "dev")] = CommitInfo("d" * 40, "dev")
        self.current = {"user": _user(1)}

    def registry(self) -> ServiceRegistryService:
        return ServiceRegistryService(
            self.session,  # type: ignore[arg-type]
            self.projects,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.targets,  # type: ignore[arg-type]
            self.installations,  # type: ignore[arg-type]
            SourceRepositoryService(self.installations, self.github),  # type: ignore[arg-type]
            self.deployments,  # type: ignore[arg-type]
            FakeTeardownService(),  # type: ignore[arg-type]
            repository_analysis_repository=self.analyses,  # type: ignore[arg-type]
        )

    def analysis_service(self) -> RepositoryAnalysisService:
        return RepositoryAnalysisService(
            self.session,  # type: ignore[arg-type]
            self.projects,  # type: ignore[arg-type]
            self.analyses,  # type: ignore[arg-type]
            self.installations,  # type: ignore[arg-type]
            SourceRepositoryService(self.installations, self.github),  # type: ignore[arg-type]
            self.registry(),
            self.manual,  # type: ignore[arg-type]
        )

    def finish(self, analysis_id: int, result: dict[str, Any]) -> RepositoryAnalysis:
        """Build Worker 가 분석을 끝낸 것처럼 결과를 기록한다."""
        analysis = self.analyses.analyses[analysis_id]
        analysis.succeed(
            AnalysisGateDecision(result["decision"]),
            AnalysisGateComplexity(result["complexity"]),
            result,
        )
        return analysis


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    s = Setup()
    installation = await s.installations.save(make_installation(5, 22, "iris-org"))
    await s.installations.replace_user_links(1, {installation.id})
    await s.installations.replace_user_links(2, {installation.id})
    await s.projects.save(Project(name="shop", owner_id=1))
    app.dependency_overrides[get_current_user] = lambda: s.current["user"]
    app.dependency_overrides[get_service_registry_service] = s.registry
    app.dependency_overrides[get_repository_analysis_service] = s.analysis_service
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
async def client(setup: Setup) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


async def _create(client: AsyncClient, **body: Any) -> dict[str, Any]:
    payload = {"sourceRepositoryUrl": REPOSITORY_URL, "sourceBranch": "main", **body}
    response = await client.post("/api/v1/projects/1/repository-analyses", json=payload)
    assert response.status_code == 202, response.text
    return dict(response.json()["data"])


async def _apply(client: AsyncClient, analysis_id: int, **body: Any) -> Any:
    payload = {"units": [{"unitId": "web"}, {"unitId": "api"}, {"unitId": "worker"}], **body}
    return await client.post(
        f"/api/v1/projects/1/repository-analyses/{analysis_id}/apply", json=payload
    )


async def test_create_analysis_returns_accepted_with_pinned_sha(
    client: AsyncClient, setup: Setup
) -> None:
    data = await _create(client, rootDirectory="/apps/", mode="force")

    assert data["status"] == "QUEUED"
    assert data["sourceSha"] == SHA
    assert data["sourceBranch"] == "main"
    assert data["rootDirectory"] == "apps"
    assert data["mode"] == "force"
    assert data["projectId"] == 1
    assert "decision" not in data and "result" not in data
    stored = setup.analyses.analyses[data["id"]]
    assert stored.user_id == 1
    assert stored.github_installation_id == 5
    assert setup.session.commit_count == 1


async def test_create_analysis_defaults_to_default_branch_and_auto(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/projects/1/repository-analyses", json={"sourceRepositoryUrl": REPOSITORY_URL}
    )

    data = response.json()["data"]
    assert response.status_code == 202
    assert (data["sourceBranch"], data["mode"], data["sourceSha"]) == ("main", "auto", SHA)
    assert "rootDirectory" not in data


async def test_create_analysis_in_other_users_project_returns_not_found(
    client: AsyncClient, setup: Setup
) -> None:
    setup.current["user"] = _user(2)

    response = await client.post(
        "/api/v1/projects/1/repository-analyses", json={"sourceRepositoryUrl": REPOSITORY_URL}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"


async def test_create_analysis_unknown_branch_returns_invalid_input(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/projects/1/repository-analyses",
        json={"sourceRepositoryUrl": REPOSITORY_URL, "sourceBranch": "ghost"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_INPUT"


async def test_create_analysis_inaccessible_repository_returns_forbidden(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/projects/1/repository-analyses",
        json={"sourceRepositoryUrl": "https://github.com/stranger/repo"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "REPOSITORY_NOT_ACCESSIBLE"


async def test_create_analysis_other_installation_returns_invalid_input(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/projects/1/repository-analyses",
        json={"sourceRepositoryUrl": REPOSITORY_URL, "githubInstallationId": 99},
    )

    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == "githubInstallationId"


async def test_create_analysis_rejects_unknown_mode(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/projects/1/repository-analyses",
        json={"sourceRepositoryUrl": REPOSITORY_URL, "mode": "deep"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_analysis_requires_login(client: AsyncClient) -> None:
    app.dependency_overrides.pop(get_current_user)

    response = await client.post(
        "/api/v1/projects/1/repository-analyses", json={"sourceRepositoryUrl": REPOSITORY_URL}
    )

    assert response.status_code in (401, 503)


async def test_get_analysis_returns_result_verbatim(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await client.get(f"/api/v1/projects/1/repository-analyses/{created['id']}")

    data = response.json()["data"]
    assert response.status_code == 200
    assert (data["status"], data["decision"], data["complexity"]) == (
        "SUCCEEDED",
        "analyze",
        "complex",
    )
    assert data["result"] == complex_result()


async def test_get_failed_analysis_returns_error(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.analyses.analyses[created["id"]].fail(AnalysisErrorCode.ANALYZER_FAILED, "boom")

    data = (await client.get(f"/api/v1/projects/1/repository-analyses/{created['id']}")).json()[
        "data"
    ]

    assert (data["status"], data["errorCode"], data["errorMessage"]) == (
        "FAILED",
        "ANALYZER_FAILED",
        "boom",
    )


async def test_get_analysis_of_other_user_returns_not_found(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.current["user"] = _user(2)

    response = await client.get(f"/api/v1/projects/1/repository-analyses/{created['id']}")

    assert response.status_code == 404


async def test_get_unknown_analysis_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/v1/projects/1/repository-analyses/42")

    assert response.status_code == 404
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_FOUND"


async def test_get_analysis_from_other_project_returns_not_found(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    await setup.projects.save(Project(name="second", owner_id=1))

    response = await client.get(f"/api/v1/projects/2/repository-analyses/{created['id']}")

    assert response.status_code == 404
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_FOUND"


async def test_apply_creates_service_per_unit_and_deploys_pinned_sha(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(
        client,
        created["id"],
        units=[
            {"unitId": "web", "name": "shop-web"},
            {"unitId": "api", "port": 8080},
            {"unitId": "worker"},
        ],
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["analysisId"] == created["id"]
    by_name = {s["name"]: s for s in data["services"]}
    assert set(by_name) == {"shop-web", "shop-api", "worker"}
    web, api, worker = by_name["shop-web"], by_name["shop-api"], by_name["worker"]
    assert (web["rootDirectory"], web["builder"], web["dockerfilePath"], web["port"]) == (
        "web",
        "dockerfile",
        "Dockerfile",
        3000,
    )
    assert (api["rootDirectory"], api["dockerfilePath"], api["port"], api["startCommand"]) == (
        "services/api",
        "docker/Dockerfile.prod",
        8080,
        "node dist/server.js",
    )
    assert (worker["builder"], worker["buildCommand"], worker["startCommand"]) == (
        "railpack",
        "pip install .",
        "python -m worker",
    )
    assert "dockerfilePath" not in worker and "port" not in worker
    for service in data["services"]:
        assert service["sourceRepositoryUrl"] == REPOSITORY_URL
        assert service["sourceBranch"] == "main"
        assert service["targetIds"] == [1]
    assert api["analysisGate"] == {
        "analysisId": created["id"],
        "decision": "analyze",
        "complexity": "complex",
        "unitId": "api",
    }
    stored_api = setup.services.services[api["id"]]
    assert stored_api.analysis_plan is not None
    assert stored_api.analysis_plan["gate"]["sourceSha"] == SHA
    assert stored_api.analysis_plan["unit"]["env"][0]["key"] == "DATABASE_URL"
    assert stored_api.analysis_plan["unit"]["applied"]["port"] == 8080
    analysis = setup.analyses.analyses[created["id"]]
    assert analysis.status == "APPLIED"
    assert analysis.applied_service_ids == [web["id"], api["id"], worker["id"]]
    assert [(c["service_id"], c["source_sha"], c["trigger_type"]) for c in setup.manual.calls] == [
        (web["id"], SHA, "MANUAL"),
        (api["id"], SHA, "MANUAL"),
        (worker["id"], SHA, "MANUAL"),
    ]


async def test_apply_again_returns_same_services_without_creating_more(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())
    first = (await _apply(client, created["id"])).json()["data"]

    second = await _apply(client, created["id"], units=[{"unitId": "web"}])

    assert second.status_code == 201
    assert [s["id"] for s in second.json()["data"]["services"]] == [
        s["id"] for s in first["services"]
    ]
    assert len(setup.services.services) == 3
    keys = {c["idempotency_key"] for c in setup.manual.calls}
    assert keys == {f"analysis-{created['id']}"}
    # 다시 보낸 배포 요청은 같은 키라 처음 만든 요청이 그대로 돌아온다.
    assert len(setup.manual.requests) == 3


async def test_apply_applies_target_and_auto_deploy_to_every_service(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(client, created["id"], targetIds=[2], isAutoDeploy=False)

    services = response.json()["data"]["services"]
    assert response.status_code == 201
    assert [(s["targetIds"], s["isAutoDeploy"]) for s in services] == [([2], False)] * 3


async def test_apply_defaults_to_auto_deploy(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    services = (await _apply(client, created["id"])).json()["data"]["services"]

    assert all(s["isAutoDeploy"] is True for s in services)


async def test_apply_without_deploy_does_not_request_deployment(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(client, created["id"], deploy=False)

    assert response.status_code == 201
    assert setup.manual.calls == []


async def test_apply_queued_analysis_returns_conflict(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)

    response = await _apply(client, created["id"])

    assert response.status_code == 409
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_READY"
    assert setup.services.services == {}


async def test_apply_skip_decision_returns_conflict(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], simple_result())

    response = await _apply(client, created["id"])

    assert response.status_code == 409
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_READY"


async def test_apply_unknown_unit_creates_nothing(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(client, created["id"], units=[{"unitId": "web"}, {"unitId": "db"}])

    assert response.status_code == 422
    assert setup.services.services == {}
    assert setup.analyses.analyses[created["id"]].status == "SUCCEEDED"


async def test_apply_duplicate_names_returns_conflict_and_creates_nothing(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(
        client,
        created["id"],
        units=[{"unitId": "web", "name": "app"}, {"unitId": "api", "name": "app"}],
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SERVICE_NAME_CONFLICT"
    assert setup.services.services == {}


async def test_apply_name_taken_in_project_returns_conflict(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())
    taken = await client.post(
        "/api/v1/projects/1/services", json={"repositoryUrl": REPOSITORY_URL, "name": "web"}
    )
    assert taken.status_code == 201

    response = await _apply(client, created["id"], units=[{"unitId": "web"}])

    assert response.status_code == 409
    assert response.json()["code"] == "SERVICE_NAME_CONFLICT"


async def test_apply_rejects_dockerfile_path_outside_unit(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(
        client, created["id"], units=[{"unitId": "web", "dockerfilePath": "../Dockerfile"}]
    )

    assert response.status_code == 422


async def test_apply_by_other_user_returns_not_found(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())
    setup.current["user"] = _user(2)

    response = await _apply(client, created["id"])

    assert response.status_code == 404


async def test_apply_requires_units(client: AsyncClient, setup: Setup) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    response = await _apply(client, created["id"], units=[])

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_service_with_skip_analysis_uses_simple_build(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], simple_result())

    response = await client.post(
        "/api/v1/projects/1/services",
        json={"repositoryUrl": REPOSITORY_URL, "analysisId": created["id"]},
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["builder"], data["dockerfilePath"]) == ("dockerfile", "Dockerfile.web")
    assert data["analysisGate"] == {
        "analysisId": created["id"],
        "decision": "skip",
        "complexity": "simple",
    }
    stored = setup.services.services[data["id"]]
    assert stored.analysis_plan == {
        "gate": {
            "analysisId": created["id"],
            "decision": "skip",
            "complexity": "simple",
            "unitId": None,
            "sourceSha": SHA,
        }
    }
    # 단순 레포는 apply 하지 않는다. 분석은 SUCCEEDED 로 남는다.
    assert setup.analyses.analyses[created["id"]].status == "SUCCEEDED"
    listed = (await client.get("/api/v1/projects/1/services")).json()["data"]
    assert listed[0]["analysisGate"]["decision"] == "skip"


async def test_create_service_with_railpack_skip_leaves_dockerfile_empty(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(
        created["id"],
        {**simple_result(), "simpleBuild": {"builder": "railpack", "dockerfilePath": None}},
    )

    data = (
        await client.post(
            "/api/v1/projects/1/services",
            json={"repositoryUrl": REPOSITORY_URL, "analysisId": created["id"]},
        )
    ).json()["data"]

    assert data["builder"] == "railpack"
    assert "dockerfilePath" not in data


async def test_create_service_with_analyze_decision_records_gate_only(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)
    setup.finish(created["id"], complex_result())

    data = (
        await client.post(
            "/api/v1/projects/1/services",
            json={"repositoryUrl": REPOSITORY_URL, "analysisId": created["id"]},
        )
    ).json()["data"]

    assert "builder" not in data
    assert data["analysisGate"]["decision"] == "analyze"


async def test_create_service_without_analysis_has_no_gate(client: AsyncClient) -> None:
    data = (
        await client.post("/api/v1/projects/1/services", json={"repositoryUrl": REPOSITORY_URL})
    ).json()["data"]

    assert "analysisGate" not in data


async def test_create_service_with_unfinished_analysis_returns_conflict(
    client: AsyncClient, setup: Setup
) -> None:
    created = await _create(client)

    response = await client.post(
        "/api/v1/projects/1/services",
        json={"repositoryUrl": REPOSITORY_URL, "analysisId": created["id"]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_READY"
    assert setup.services.services == {}


@pytest.mark.parametrize(
    "body",
    [
        {"repositoryUrl": "https://github.com/iris-org/other"},
        {"repositoryUrl": REPOSITORY_URL, "rootDirectory": "web"},
    ],
    ids=["other-repository", "other-root"],
)
async def test_create_service_with_analysis_of_other_source_returns_invalid_input(
    client: AsyncClient, setup: Setup, body: dict[str, Any]
) -> None:
    created = await _create(client)
    setup.finish(created["id"], simple_result())

    response = await client.post(
        "/api/v1/projects/1/services", json={**body, "analysisId": created["id"]}
    )

    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == "analysisId"


async def test_create_service_with_unknown_analysis_returns_not_found(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/projects/1/services", json={"repositoryUrl": REPOSITORY_URL, "analysisId": 9}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "REPOSITORY_ANALYSIS_NOT_FOUND"
