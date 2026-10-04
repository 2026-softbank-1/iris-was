"""Tailscale API v2 Client. Deploy Worker 가 지운 온프레미스 서버의 tailnet 기기를 정리할 때 쓴다.

http 에 base_url(`https://api.tailscale.com`)과 `Authorization: Bearer <API 키>` 를 설정해 넘긴다.
키는 이 모듈에서 다루지 않고 오류·로그에도 남기지 않는다.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.exceptions import ExternalError

TAILSCALE_API_URL = "https://api.tailscale.com"


class TailscaleApiError(ExternalError):
    """Tailscale API 호출 실패. 응답 본문은 옮기지 않고 상태 코드만 fields 에 담는다."""


@dataclass(frozen=True)
class TailscaleDevice:
    id: str
    # 기기가 알린 hostname(`tailscale up --hostname`). 설치 스크립트는 `iris-{serverKey}` 다.
    hostname: str
    # MagicDNS 이름(`iris-{key}.<tailnet>.ts.net`).
    name: str
    tags: tuple[str, ...]

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> "TailscaleDevice":
        return cls(
            id=str(body.get("id", "")),
            hostname=str(body.get("hostname", "")),
            name=str(body.get("name", "")),
            tags=tuple(str(tag) for tag in body.get("tags") or ()),
        )


class TailscaleClient:
    def __init__(self, http: httpx.AsyncClient, tailnet: str) -> None:
        self._http = http
        # `-` 는 API 키가 속한 기본 tailnet 이다.
        self._tailnet = tailnet

    async def search_devices(self) -> list[TailscaleDevice]:
        response = await self._send("GET", f"/api/v2/tailnet/{self._tailnet}/devices")
        devices = response.json().get("devices") or []
        return [TailscaleDevice.from_response(device) for device in devices]

    async def delete_device(self, device_id: str) -> None:
        """기기를 tailnet 에서 지운다. 이미 없으면(404) 그대로 끝낸다."""
        await self._send("DELETE", f"/api/v2/device/{device_id}", allowed_statuses=(404,))

    async def _send(
        self, method: str, path: str, *, allowed_statuses: tuple[int, ...] = ()
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path)
        except httpx.HTTPError as exc:
            raise TailscaleApiError("tailscale request failed", method=method) from exc
        if response.is_error and response.status_code not in allowed_statuses:
            raise TailscaleApiError(
                "tailscale request failed", method=method, status_code=response.status_code
            )
        return response
