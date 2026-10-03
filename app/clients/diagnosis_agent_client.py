"""에러 진단 에이전트 서버(iris-error-check-agent)의 `POST /diagnose` 호출.

에이전트는 인증 실패·입력 오류·모델 오류를 `{"error": {"code", "message"}}` 가 든 JSON 으로
돌려준다. message 는 입력 일부를 담을 수 있어 옮기지 않고 code 만 예외에 싣는다.
"""

from typing import Any, Protocol

import httpx

from app.core.exceptions import DiagnosisAgentError


class DiagnosisAgentClient(Protocol):
    async def diagnose(self, data: dict[str, Any]) -> dict[str, Any]:
        """진단 결과(diagnosis-result.v3)를 돌려준다. 실패하면 DiagnosisAgentError 를 던진다."""
        ...


class HttpDiagnosisAgentClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, api_key: str, timeout_seconds: float
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def diagnose(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/diagnose",
                json=data,
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise DiagnosisAgentError("diagnosis agent timed out", agent_code="TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise DiagnosisAgentError(
                "diagnosis agent request failed", reason=type(exc).__name__
            ) from exc

        body = _parse_object(response)
        if response.status_code == 200:
            if body is None:
                raise DiagnosisAgentError(
                    "diagnosis agent returned an invalid response",
                    agent_code="INVALID_RESPONSE",
                    agent_status=response.status_code,
                )
            return body
        raise DiagnosisAgentError(
            "diagnosis agent rejected the api key"
            if response.status_code == 401
            else "diagnosis agent failed",
            agent_code=_error_code(body),
            agent_status=response.status_code,
        )


def _parse_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error_code(body: dict[str, Any] | None) -> str | None:
    error = body.get("error") if body is not None else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None
