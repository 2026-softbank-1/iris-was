"""Internal repair API and bounded trusted snapshot HTTP clients."""

import asyncio
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.core.exceptions import ExternalError, InvalidInputError
from app.services.repair_source import MAX_ARCHIVE_BYTES, PinnedRepairSource, pin_archive

ARTIFACT_NAMES = frozenset({"patch.diff", "changes.json", "manifest.json"})
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class RepairAgentError(ExternalError):
    code = "REPAIR_AGENT_ERROR"
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        agent_code: str | None = None,
        agent_status: int | None = None,
        **fields: object,
    ) -> None:
        super().__init__(message, agent_code=agent_code, agent_status=agent_status, **fields)
        self.agent_code = agent_code
        self.agent_status = agent_status


class RepairAgentClient(Protocol):
    async def submit(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]: ...
    async def get_receipt(self, request_id: str) -> dict[str, Any]: ...
    async def get_artifact(self, request_id: str, name: str) -> bytes: ...


class RepairSourceClient(Protocol):
    async def pin_source(self, download_url: str) -> PinnedRepairSource: ...


def _validate_request_id(request_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
        raise InvalidInputError("invalid repair request identifier")


class HttpRepairAgentClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, api_key: str, timeout_seconds: float
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self._timeout = timeout_seconds

    async def submit(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        _validate_request_id(request_id)
        if payload.get("requestId") != request_id:
            raise InvalidInputError("repair idempotency key must equal requestId")
        try:
            response = await self._http.post(
                f"{self._base_url}/internal/repairs",
                json=payload,
                headers={**self._headers, "Idempotency-Key": request_id},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            # One read can recover a completed attempt. Never repost after uncertainty.
            try:
                receipt = await self.get_receipt(request_id)
            except RepairAgentError:
                raise RepairAgentError(
                    "repair outcome is uncertain", agent_code="UNKNOWN_OUTCOME"
                ) from None
            result = receipt.get("result")
            if receipt.get("status") == "SUCCEEDED" and isinstance(result, dict):
                return result
            return receipt
        except httpx.HTTPError:
            raise RepairAgentError("repair request failed", agent_code="UNKNOWN_OUTCOME") from None
        return self._object(response, allow_conflict=True)

    async def get_receipt(self, request_id: str) -> dict[str, Any]:
        _validate_request_id(request_id)
        try:
            response = await self._http.get(
                f"{self._base_url}/internal/repairs/{request_id}",
                headers=self._headers,
                timeout=min(self._timeout, 10),
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise RepairAgentError(
                "repair receipt lookup failed", agent_code="UNKNOWN_OUTCOME"
            ) from None
        return self._object(response)

    async def get_artifact(self, request_id: str, name: str) -> bytes:
        _validate_request_id(request_id)
        if name not in ARTIFACT_NAMES:
            raise InvalidInputError("invalid repair artifact name")
        try:
            async with self._http.stream(
                "GET",
                f"{self._base_url}/internal/repairs/{request_id}/artifacts/{name}",
                headers=self._headers,
                timeout=min(self._timeout, 30),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise RepairAgentError(
                        "repair artifact download failed", agent_status=response.status_code
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_ARTIFACT_BYTES:
                        raise RepairAgentError(
                            "repair artifact exceeds limit", agent_code="INVALID_ARTIFACT"
                        )
                return bytes(content)
        except httpx.HTTPError:
            raise RepairAgentError("repair artifact download failed") from None

    @staticmethod
    def _object(response: httpx.Response, *, allow_conflict: bool = False) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and (
            response.status_code == 200
            or (
                allow_conflict
                and response.status_code == 409
                and body.get("status") in {"RUNNING", "UNKNOWN_OUTCOME", "FAILED"}
            )
        ):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        # Never propagate provider messages, signed URLs or arbitrary error code text.
        safe_code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code)
            else "INVALID_RESPONSE"
        )
        raise RepairAgentError(
            "repair agent rejected request", agent_code=safe_code, agent_status=response.status_code
        )


class HttpRepairSourceClient:
    def __init__(self, http: httpx.AsyncClient, allowed_hosts: tuple[str, ...]) -> None:
        self._http = http
        self._hosts = frozenset(host.lower() for host in allowed_hosts)

    async def pin_source(self, download_url: str) -> PinnedRepairSource:
        try:
            parsed = urlsplit(download_url)
            valid = (
                parsed.scheme == "https"
                and parsed.port in (None, 443)
                and parsed.hostname in self._hosts
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise InvalidInputError("repair source host is not trusted")
        try:
            async with self._http.stream(
                "GET", download_url, timeout=30, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise RepairAgentError("repair source download failed")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_ARCHIVE_BYTES:
                        raise InvalidInputError("repair source archive exceeds size limit")
        except httpx.HTTPError:
            raise RepairAgentError("repair source download failed") from None
        return await asyncio.to_thread(pin_archive, bytes(data))
