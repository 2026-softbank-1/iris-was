from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import DiagnosisAgentError
from app.dependencies import get_current_user, get_diagnosis_service, get_diagnosis_service_opener
from app.enums import DeploymentStatus, DiagnosisStatus
from app.main import app
from app.models.user import User
from tests.fakes_diagnosis import OWNER, DiagnosisSetup, valid_agent_result


def _user(id_: int) -> User:
    user = User(github_id=1000 + id_, login=f"user{id_}")
    user.id = id_
    return user


class DiagnosisClient(AsyncClient):
    setup: DiagnosisSetup
    current: dict[str, User]
    agent_configured: dict[str, bool]

    def url(self, deployment_id: int, action: str) -> str:
        return f"/api/v1/services/{self.setup.service.id}/deployments/{deployment_id}/{action}"


@pytest.fixture
async def client() -> AsyncIterator[DiagnosisClient]:
    setup = await DiagnosisSetup().build()
    current = {"user": _user(OWNER)}
    agent_configured = {"value": True}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_diagnosis_service] = lambda: setup.diagnosis_service(
        agent=agent_configured["value"]
    )
    app.dependency_overrides[get_diagnosis_service_opener] = lambda: setup.diagnosis_service_opener(
        agent=agent_configured["value"]
    )
    # ASGITransport 는 백그라운드 작업이 끝난 뒤 응답을 돌려준다. POST 다음 GET 은 결과를 본다.
    async with DiagnosisClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        http.current = current
        http.agent_configured = agent_configured
        yield http
    app.dependency_overrides.clear()


async def test_diagnose_accepts_and_answers_running_then_result_is_polled(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()

    started = await client.post(client.url(request.id, "diagnose"))

    assert started.status_code == 202
    assert started.json()["success"] is True
    running = started.json()["data"]
    assert running["deploymentId"] == request.id
    assert running["status"] == "RUNNING"
    assert "analysis" not in running and "finishedAt" not in running
    assert len(client.setup.agent.requests) == 1

    polled = await client.get(client.url(request.id, "diagnosis"))

    data = polled.json()["data"]
    assert polled.status_code == 200
    assert data["id"] == running["id"]
    assert data["status"] == "SUCCEEDED"
    assert "errorCode" not in data
    analysis = data["analysis"]
    assert analysis["analysisStatus"] == "diagnosed"
    assert analysis["summary"].startswith("DATABASE_URL")
    hypothesis = analysis["hypotheses"][0]
    assert hypothesis["supportLevel"] == "direct"
    assert hypothesis["evidenceIds"] == ["EV000001"]
    plan = analysis["remediation"]["plans"][0]
    assert plan["title"] == "DATABASE_URL 변수 추가"
    assert plan["changes"][0]["snippetKind"] == "template"
    assert plan["changes"][0]["placeholders"][0]["name"] == "DATABASE_URL"
    assert plan["verification"][0]["expectedResult"] == "앱이 시작한다."
    assert data["evidence"][0]["id"] == "EV000001"
    assert data["evidence"][0]["sourceId"] == "app-0"
    assert data["sourceAnalysis"]["status"] == "not_needed"
    assert "result" not in data and "execution" not in data
    assert "finishedAt" in data


async def test_diagnose_again_returns_saved_result_with_ok_status_until_refresh(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    url = client.url(request.id, "diagnose")
    first = await client.post(url)

    second = await client.post(url)
    refreshed = await client.post(url, params={"refresh": "true"})

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert second.json()["data"]["status"] == "SUCCEEDED"
    assert refreshed.status_code == 202
    assert refreshed.json()["data"]["id"] != first.json()["data"]["id"]
    assert len(client.setup.agent.requests) == 2


async def test_diagnose_succeeded_deployment_returns_conflict(client: DiagnosisClient) -> None:
    request = client.setup.add_request(DeploymentStatus.SUCCEEDED, failure_code=None)

    response = await client.post(client.url(request.id, "diagnose"))

    assert response.status_code == 409
    assert response.json()["code"] == "DEPLOYMENT_NOT_FAILED"


async def test_diagnose_while_running_returns_conflict(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)

    response = await client.post(client.url(request.id, "diagnose"))

    assert response.status_code == 409
    assert response.json()["code"] == "DIAGNOSIS_IN_PROGRESS"
    assert client.setup.agent.requests == []


async def test_diagnose_without_runtime_logs_ends_failed_with_error_code(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    client.setup.loki.entries = []

    started = await client.post(client.url(request.id, "diagnose"))
    polled = await client.get(client.url(request.id, "diagnosis"))

    assert started.status_code == 202
    data = polled.json()["data"]
    assert data["status"] == "FAILED"
    assert data["errorCode"] == "DIAGNOSIS_LOGS_UNAVAILABLE"
    assert "analysis" not in data
    assert client.setup.agent.requests == []


async def test_diagnose_agent_failure_ends_failed_with_agent_code(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_TIMEOUT")]

    started = await client.post(client.url(request.id, "diagnose"))
    polled = await client.get(client.url(request.id, "diagnosis"))

    assert started.status_code == 202
    data = polled.json()["data"]
    assert data["status"] == "FAILED"
    assert data["errorCode"] == "MODEL_TIMEOUT"
    assert "analysis" not in data


async def test_diagnose_unexpected_crash_ends_failed_without_failing_the_response(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    client.setup.agent.responses = [RuntimeError("boom")]

    started = await client.post(client.url(request.id, "diagnose"))
    polled = await client.get(client.url(request.id, "diagnosis"))

    assert started.status_code == 202
    assert polled.json()["data"]["status"] == "FAILED"
    assert polled.json()["data"]["errorCode"] == "INTERNAL_ERROR"


async def test_diagnose_failed_run_can_be_started_again(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.setup.agent.responses = [DiagnosisAgentError("failed", agent_code="MODEL_TIMEOUT")]
    await client.post(client.url(request.id, "diagnose"))
    client.setup.agent.responses = [valid_agent_result()]

    retried = await client.post(client.url(request.id, "diagnose"))
    polled = await client.get(client.url(request.id, "diagnosis"))

    assert retried.status_code == 202
    assert polled.json()["data"]["status"] == "SUCCEEDED"


async def test_diagnose_without_agent_settings_returns_not_configured(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    client.agent_configured["value"] = False

    response = await client.post(client.url(request.id, "diagnose"))

    assert response.status_code == 503
    assert response.json()["code"] == "NOT_CONFIGURED"
    assert client.setup.diagnoses.rows == []


async def test_diagnose_of_other_users_service_returns_not_found(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.current["user"] = _user(OWNER + 1)

    response = await client.post(client.url(request.id, "diagnose"))

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_diagnose_unknown_deployment_returns_not_found(client: DiagnosisClient) -> None:
    response = await client.post(client.url(999, "diagnose"))

    assert response.status_code == 404
    assert response.json()["code"] == "DEPLOYMENT_REQUEST_NOT_FOUND"


async def test_get_diagnosis_running_has_no_result_yet(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)

    response = await client.get(client.url(request.id, "diagnosis"))

    assert response.json()["data"]["status"] == "RUNNING"
    assert "analysis" not in response.json()["data"]


async def test_get_diagnosis_before_any_run_returns_not_found(client: DiagnosisClient) -> None:
    request = client.setup.add_request()

    response = await client.get(client.url(request.id, "diagnosis"))

    assert response.status_code == 404
    assert response.json()["code"] == "DIAGNOSIS_NOT_FOUND"


async def test_get_diagnosis_works_without_agent_settings(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    client.setup.diagnoses.seed(request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result())
    client.agent_configured["value"] = False

    response = await client.get(client.url(request.id, "diagnosis"))

    assert response.status_code == 200
    assert response.json()["data"]["analysis"]["summary"].startswith("DATABASE_URL")


async def test_get_diagnosis_of_other_users_service_returns_not_found(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    client.current["user"] = _user(OWNER + 1)

    response = await client.get(client.url(request.id, "diagnosis"))

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_repair_context_pins_diagnosis_and_disables_caching(client: DiagnosisClient) -> None:
    request = client.setup.add_request()
    build = client.setup.add_build(request)
    build.source_sha = request.source_sha
    diagnosis = client.setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )
    client.setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)
    app.dependency_overrides[get_diagnosis_service] = lambda: client.setup.diagnosis_service(
        snapshots=True
    )
    response = await client.get(
        client.url(request.id, "repair-context"), params={"diagnosisId": diagnosis.id}
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()["data"]
    assert data["diagnosisId"] == diagnosis.id
    assert data["diagnosisResult"]["schema_version"] == "diagnosis-result.v3"
    assert data["source"]["commitSha"] == request.source_sha
    client.current["user"] = _user(OWNER + 1)
    denied = await client.get(
        client.url(request.id, "repair-context"), params={"diagnosisId": diagnosis.id}
    )
    assert denied.status_code == 404
    assert "downloadUrl" not in denied.text


async def test_repair_context_validates_id_and_missing_configuration(
    client: DiagnosisClient,
) -> None:
    request = client.setup.add_request()
    diagnosis = client.setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )
    invalid = await client.get(client.url(request.id, "repair-context"), params={"diagnosisId": 0})
    assert invalid.status_code == 422
    missing = await client.get(
        client.url(request.id, "repair-context"), params={"diagnosisId": diagnosis.id}
    )
    assert missing.status_code == 503


async def test_repair_context_requires_authentication(client: DiagnosisClient) -> None:
    from app.dependencies import get_session_service

    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides[get_session_service] = lambda: object()
    response = await client.get(client.url(999, "repair-context"))
    assert response.status_code == 401
    assert "downloadUrl" not in response.text
