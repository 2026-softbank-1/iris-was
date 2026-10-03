import json

import httpx
import pytest

from app.clients.repair_agent_client import (
    HttpRepairAgentClient,
    HttpRepairSourceClient,
    RepairAgentError,
)
from app.core.exceptions import InvalidInputError


async def test_repair_timeout_reads_receipt_once_without_second_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.headers["Idempotency-Key"] == "repair-1"
            assert request.headers["X-API-Key"] == "test-key"
            assert json.loads(request.content)["requestId"] == "repair-1"
            raise httpx.ReadTimeout("secret-url", request=request)
        return httpx.Response(200, json={"status": "SUCCEEDED", "result": {"status": "no_change"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HttpRepairAgentClient(http, "http://agent", "test-key", 2)
        assert await client.submit({"requestId": "repair-1"}, "repair-1") == {"status": "no_change"}
    assert [request.method for request in requests] == ["POST", "GET"]


async def test_repair_uncertain_timeout_never_reposts_or_leaks_details() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("SIGNED_SECRET", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HttpRepairAgentClient(http, "http://agent", "test-key", 2)
        with pytest.raises(RepairAgentError) as error:
            await client.submit({"requestId": "repair-1"}, "repair-1")
    assert calls == 2
    assert error.value.fields["agent_code"] == "UNKNOWN_OUTCOME"
    assert "SIGNED_SECRET" not in str(error.value)
    assert error.value.retryable is False


async def test_repair_rejects_idempotency_mismatch_before_network() -> None:
    async with httpx.AsyncClient() as http:
        client = HttpRepairAgentClient(http, "http://agent", "key", 2)
        with pytest.raises(InvalidInputError):
            await client.submit({"requestId": "other"}, "repair-1")


async def test_repair_does_not_follow_redirect_or_trust_error_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://evil.example"},
            json={"error": {"code": "BAD_INPUT", "message": "SIGNED_SECRET"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        client = HttpRepairAgentClient(http, "http://agent", "key", 2)
        with pytest.raises(RepairAgentError) as error:
            await client.submit({"requestId": "repair-1"}, "repair-1")
    assert len(requests) == 1
    assert "SIGNED_SECRET" not in str(error.value.fields)


@pytest.mark.parametrize(
    "url",
    [
        "http://source.test/a",
        "https://source.test.evil/a",
        "https://key@source.test/a",
        "https://source.test:444/a",
    ],
)
async def test_repair_source_requires_exact_trusted_https_host(url: str) -> None:
    async with httpx.AsyncClient() as http:
        client = HttpRepairSourceClient(http, ("source.test",))
        with pytest.raises(InvalidInputError):
            await client.pin_source(url)


async def test_repair_receipt_and_artifact_have_short_read_deadlines() -> None:
    deadlines: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        deadlines.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"status": "RUNNING"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HttpRepairAgentClient(http, "http://agent", "key", 150)
        await client.get_receipt("repair-1")
        await client.get_artifact("repair-1", "patch.diff")
    assert deadlines == [10, 30]
