from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user, get_repair_context_service
from app.enums import DiagnosisStatus
from app.main import app
from app.models.user import User
from app.services.repair_context_service import RepairContextService
from tests.fakes_diagnosis import OWNER, DiagnosisSetup, valid_agent_result


class RepairClient(AsyncClient):
    setup: DiagnosisSetup

    def url(self, deployment_id: int) -> str:
        return (
            f"/api/v1/services/{self.setup.service.id}/deployments/{deployment_id}/repair-context"
        )


@pytest.fixture
async def client() -> AsyncIterator[RepairClient]:
    setup = await DiagnosisSetup().build()
    user = User(github_id=1000 + OWNER, login=f"user{OWNER}")
    user.id = OWNER
    service = RepairContextService(
        setup.services,  # type: ignore[arg-type]
        setup.requests,  # type: ignore[arg-type]
        setup.builds,  # type: ignore[arg-type]
        setup.diagnoses,  # type: ignore[arg-type]
        setup.snapshots,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_repair_context_service] = lambda: service
    async with RepairClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        http.setup = setup
        yield http
    app.dependency_overrides.clear()


async def test_get_repair_context_returns_camel_case_envelope_and_untouched_diagnosis(
    client: RepairClient,
) -> None:
    request = client.setup.add_request()
    build = client.setup.add_build(request)
    build.record_source_digests("a" * 64, "b" * 64)
    diagnosis = client.setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    response = await client.get(client.url(request.id), params={"diagnosisId": diagnosis.id})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["deploymentId"] == request.id and data["diagnosisId"] == diagnosis.id
    assert data["autoDeploy"] is True and data["branch"]
    source = data["source"]
    assert source["archiveSha256"] == "a" * 64 and source["manifestSha256"] == "b" * 64
    assert source["rootDirectory"] == "."
    assert source["downloadUrl"].startswith("https://")
    # 진단 원문은 snake_case 키와 null 이 그대로다.
    assert data["diagnosisResult"] == valid_agent_result()
    assert data["diagnosisResult"]["error"] is None


async def test_get_repair_context_requires_diagnosis_id(client: RepairClient) -> None:
    request = client.setup.add_request()

    response = await client.get(client.url(request.id))

    assert response.status_code == 422


async def test_get_repair_context_unsucceeded_diagnosis_is_conflict(
    client: RepairClient,
) -> None:
    request = client.setup.add_request()
    client.setup.add_build(request)
    diagnosis = client.setup.diagnoses.seed(request.id, DiagnosisStatus.RUNNING)

    response = await client.get(client.url(request.id), params={"diagnosisId": diagnosis.id})

    assert response.status_code == 409
    assert response.json()["code"] == "DIAGNOSIS_NOT_SUCCEEDED"


async def test_get_repair_context_expired_snapshot_is_conflict(client: RepairClient) -> None:
    request = client.setup.add_request()
    diagnosis = client.setup.diagnoses.seed(
        request.id, DiagnosisStatus.SUCCEEDED, result=valid_agent_result()
    )

    response = await client.get(client.url(request.id), params={"diagnosisId": diagnosis.id})

    assert response.status_code == 409
    assert response.json()["code"] == "SOURCE_SNAPSHOT_UNAVAILABLE"
