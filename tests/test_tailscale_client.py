import httpx
import pytest

from app.clients.tailscale_client import TailscaleApiError, TailscaleClient, TailscaleDevice
from app.services.onprem_server_sync_service import select_server_devices

API_KEY = "tskey-api-must-not-leak"


def _client(handler: httpx.MockTransport) -> TailscaleClient:
    http = httpx.AsyncClient(
        transport=handler,
        base_url="https://api.tailscale.com",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    return TailscaleClient(http, "-")


async def test_search_devices_reads_id_hostname_name_and_tags() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "devices": [
                    {
                        "id": "123",
                        "hostname": "iris-k3x9q2ma",
                        "name": "iris-k3x9q2ma.tailb046e8.ts.net",
                        "tags": ["tag:iris-onprem"],
                    },
                    {"id": "456", "hostname": "laptop", "name": "laptop.tailb046e8.ts.net"},
                ]
            },
        )

    devices = await _client(httpx.MockTransport(handle)).search_devices()

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v2/tailnet/-/devices"
    assert seen[0].headers["Authorization"] == f"Bearer {API_KEY}"
    assert devices == [
        TailscaleDevice(
            "123", "iris-k3x9q2ma", "iris-k3x9q2ma.tailb046e8.ts.net", ("tag:iris-onprem",)
        ),
        TailscaleDevice("456", "laptop", "laptop.tailb046e8.ts.net", ()),
    ]


@pytest.mark.parametrize("status_code", [200, 404])
async def test_delete_device_treats_missing_device_as_deleted(status_code: int) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code)

    await _client(httpx.MockTransport(handle)).delete_device("123")

    assert (seen[0].method, seen[0].url.path) == ("DELETE", "/api/v2/device/123")


@pytest.mark.parametrize(
    "handle",
    [
        lambda request: httpx.Response(403, json={"message": f"bad key {API_KEY}"}),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
async def test_tailscale_errors_become_external_error_without_key(handle: object) -> None:
    client = _client(httpx.MockTransport(handle))  # type: ignore[arg-type]

    with pytest.raises(TailscaleApiError) as error:
        await client.delete_device("123")

    assert error.value.status_code == 502
    assert API_KEY not in str(error.value) + repr(error.value.fields)


def test_select_server_devices_needs_both_hostname_and_onprem_tag() -> None:
    devices = [
        TailscaleDevice("1", "iris-k3x9q2ma", "iris-k3x9q2ma.t.ts.net", ("tag:iris-onprem",)),
        TailscaleDevice("2", "iris-k3x9q2ma", "iris-k3x9q2ma-1.t.ts.net", ()),
        TailscaleDevice("3", "iris-other000", "iris-other000.t.ts.net", ("tag:iris-onprem",)),
        TailscaleDevice("4", "iris-k3x9q2ma-1", "x.t.ts.net", ("tag:iris-onprem",)),
        TailscaleDevice("5", "iris-k3x9q2ma", "y.t.ts.net", ("tag:other", "tag:iris-onprem")),
    ]

    assert [d.id for d in select_server_devices(devices, "k3x9q2ma")] == ["1", "5"]
