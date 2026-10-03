import json

import httpx
import pytest

from app.clients.observability_client import LokiPrometheusObservabilityClient
from app.core.exceptions import ExternalError


def _streams(*values: list[object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {"resultType": "streams", "result": [{"stream": {}, "values": list(values)}]},
        },
    )


async def test_loki_logs_can_be_scoped_to_release_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LokiPrometheusObservabilityClient(http)
        await client.search_logs("http://loki", "svc-42", 1, 2, 10, "", release_ids=[7])
        await client.search_logs("http://loki", "svc-42", 1, 2, 10, "", release_ids=[7, 9])

    assert requests[0].url.params["query"] == (
        '{k8s_namespace_name="svc-42",k8s_container_name="app",iris_release_id=~"7"}'
    )
    assert requests[1].url.params["query"] == (
        '{k8s_namespace_name="svc-42",k8s_container_name="app",iris_release_id=~"7|9"}'
    )


async def test_network_logs_query_alb_stream_and_filter_by_status_class() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _streams()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LokiPrometheusObservabilityClient(http)
        await client.search_network_logs("http://loki", "svc-42", 1, 2, 10, None)
        await client.search_network_logs("http://loki", "svc-42", 1, 2, 10, "5xx")

    assert requests[0].url.params["query"] == '{job="iris-alb-access",k8s_namespace_name="svc-42"}'
    assert requests[1].url.params["query"] == (
        '{job="iris-alb-access",k8s_namespace_name="svc-42"}'
        ' | json | __error__ = "" | elb_status_code >= 500 | elb_status_code <= 599'
    )
    assert requests[0].url.params["direction"] == "backward"
    assert requests[0].url.params["limit"] == "10"


async def test_network_logs_parse_entries_sorted_and_hide_internal_values() -> None:
    ok = json.dumps(
        {
            "record_id": "abc",
            "elb_status_code": 200,
            "target_status_code": 200,
            "received_bytes": 120,
            "sent_bytes": 3400,
            "target_processing_time": 0.012,
            "target_group_arn": "arn:aws:elasticloadbalancing:secret",
        }
    )
    elb_error = json.dumps(
        {
            "elb_status_code": 502,
            "target_status_code": None,
            "received_bytes": 10,
            "sent_bytes": 0,
            "target_processing_time": None,
        }
    )
    values: list[list[object]] = [
        ["200", elb_error],
        ["100", ok],
        ["150", "not json"],
        ["160", json.dumps({"elb_status_code": "200", "received_bytes": 1, "sent_bytes": 1})],
        ["170", json.dumps(["list"])],
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _streams(*values))
    ) as http:
        entries = await LokiPrometheusObservabilityClient(http).search_network_logs(
            "http://loki", "svc-42", 1, 300, 10, None
        )

    assert [
        (e.timestamp_ns, e.status, e.target_status, e.received_bytes, e.sent_bytes) for e in entries
    ] == [("100", 200, 200, 120, 3400), ("200", 502, None, 10, 0)]
    assert [e.response_time_seconds for e in entries] == [0.012, None]
    assert not hasattr(entries[0], "target_group_arn")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"status": "success", "data": {"resultType": "vector"}}),
        _streams([100, "timestamp must be a string"]),
    ],
)
async def test_network_log_failures_are_domain_errors(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ExternalError):
            await LokiPrometheusObservabilityClient(http).search_network_logs(
                "http://loki", "svc-42", 1, 2, 10, None
            )
