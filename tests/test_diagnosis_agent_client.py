import json
from typing import Any

import httpx
import pytest

from app.clients.diagnosis_agent_client import HttpDiagnosisAgentClient
from app.core.exceptions import DiagnosisAgentError, ExternalError

API_KEY = "k" * 40


def _client(handler: Any, base_url: str = "http://agent.internal") -> tuple[Any, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpDiagnosisAgentClient(http, base_url, API_KEY, timeout_seconds=5), http


async def test_diagnose_posts_data_with_api_key_and_returns_result() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"job_status": "succeeded"})

    client, http = _client(handler, "http://agent.internal/")
    async with http:
        result = await client.diagnose({"projectId": 1, "logs": []})

    assert result == {"job_status": "succeeded"}
    assert str(requests[0].url) == "http://agent.internal/diagnose"
    assert requests[0].method == "POST"
    assert requests[0].headers["X-API-Key"] == API_KEY
    assert json.loads(requests[0].content) == {"projectId": 1, "logs": []}


async def test_diagnose_unauthorized_raises_agent_error_without_leaking_key() -> None:
    client, http = _client(
        lambda _: httpx.Response(
            401,
            json={"error": {"code": "UNAUTHORIZED", "message": "유효한 X-API-Key가 필요합니다."}},
        )
    )

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert error.value.agent_status == 401
    assert error.value.agent_code == "UNAUTHORIZED"
    assert API_KEY not in str(error.value) and API_KEY not in str(error.value.fields)


async def test_diagnose_rejected_input_keeps_agent_code_but_not_agent_message() -> None:
    client, http = _client(
        lambda _: httpx.Response(
            422, json={"error": {"code": "EMPTY_LOGS", "message": "DATABASE_URL=postgres://secret"}}
        )
    )

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert (error.value.agent_status, error.value.agent_code) == (422, "EMPTY_LOGS")
    assert "secret" not in str(error.value) and "secret" not in str(error.value.fields)


async def test_diagnose_failed_job_result_body_yields_its_error_code() -> None:
    client, http = _client(
        lambda _: httpx.Response(
            504,
            json={"job_status": "timed_out", "error": {"code": "MODEL_TIMEOUT", "message": "x"}},
        )
    )

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert (error.value.agent_status, error.value.agent_code) == (504, "MODEL_TIMEOUT")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(429, json={"error": {"code": "BUSY"}}),
        httpx.Response(500, json=["not", "an", "object"]),
    ],
)
async def test_diagnose_other_failures_are_external_errors(response: httpx.Response) -> None:
    client, http = _client(lambda _: response)

    async with http:
        with pytest.raises(ExternalError) as error:
            await client.diagnose({})

    assert error.value.status_code == 502
    assert error.value.retryable is True


async def test_diagnose_ok_status_with_invalid_json_is_invalid_response() -> None:
    client, http = _client(lambda _: httpx.Response(200, text="<html>"))

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert error.value.agent_code == "INVALID_RESPONSE"


async def test_diagnose_timeout_is_agent_error_with_timeout_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, http = _client(handler)

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert error.value.agent_code == "TIMEOUT"


async def test_diagnose_connection_error_is_agent_error_without_url_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect to agent.internal:8001 failed", request=request)

    client, http = _client(handler)

    async with http:
        with pytest.raises(DiagnosisAgentError) as error:
            await client.diagnose({})

    assert error.value.fields["reason"] == "ConnectError"
    assert "agent.internal" not in str(error.value)
