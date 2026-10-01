from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.exceptions import ExternalError, UnauthorizedError


@dataclass(frozen=True)
class OAuthUser:
    github_id: int
    login: str
    avatar_url: str | None


@dataclass(frozen=True)
class InstallationInfo:
    installation_id: int
    account_login: str
    account_type: str


class OAuthClient(Protocol):
    def build_authorization_url(self, state: str) -> str: ...

    def build_installation_url(self, app_slug: str, state: str) -> str:
        """GitHub App 설치 페이지. state 는 설치 후 돌아오는 콜백에서 그대로 확인한다."""
        ...

    async def exchange_code(self, code: str) -> str:
        """인가 코드를 사용자 액세스 토큰으로 바꾼다. 토큰은 저장하지 않고 한 번만 쓴다."""
        ...

    async def fetch_user(self, access_token: str) -> OAuthUser: ...

    async def fetch_installations(self, access_token: str) -> list[InstallationInfo]:
        """사용자가 접근할 수 있는 GitHub App 설치 목록."""
        ...


class GithubOAuthClient:
    """GitHub App 의 user authorization 흐름. 설치 접근 권한은 이 토큰으로 확인한다."""

    _PAGE_SIZE = 100
    _MAX_PAGES = 10

    def __init__(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        web_base_url: str,
        api_base_url: str,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._web_base_url = web_base_url.rstrip("/")
        self._api_base_url = api_base_url.rstrip("/")

    def build_authorization_url(self, state: str) -> str:
        query = urlencode({"client_id": self._client_id, "state": state})
        return f"{self._web_base_url}/login/oauth/authorize?{query}"

    def build_installation_url(self, app_slug: str, state: str) -> str:
        return (
            f"{self._web_base_url}/apps/{app_slug}/installations/new?{urlencode({'state': state})}"
        )

    async def exchange_code(self, code: str) -> str:
        body = await self._request(
            "POST",
            f"{self._web_base_url}/login/oauth/access_token",
            json={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
            },
        )
        # 잘못되거나 만료된 코드는 200 응답에 error 필드로 온다.
        access_token = body.get("access_token")
        if not isinstance(access_token, str):
            raise UnauthorizedError("github authorization code rejected", reason=body.get("error"))
        return access_token

    async def fetch_user(self, access_token: str) -> OAuthUser:
        body = await self._request("GET", f"{self._api_base_url}/user", token=access_token)
        return OAuthUser(
            github_id=int(body["id"]),
            login=str(body["login"]),
            avatar_url=body.get("avatar_url"),
        )

    async def fetch_installations(self, access_token: str) -> list[InstallationInfo]:
        installations: list[InstallationInfo] = []
        for page in range(1, self._MAX_PAGES + 1):
            body = await self._request(
                "GET",
                f"{self._api_base_url}/user/installations",
                token=access_token,
                params={"per_page": self._PAGE_SIZE, "page": page},
            )
            items = body.get("installations", [])
            installations.extend(
                InstallationInfo(
                    installation_id=int(item["id"]),
                    account_login=str(item["account"]["login"]),
                    account_type=str(item["account"]["type"]),
                )
                for item in items
            )
            if len(items) < self._PAGE_SIZE:
                break
        return installations

    async def _request(
        self,
        method: str,
        url: str,
        token: str | None = None,
        json: dict[str, str] | None = None,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._http.request(
                method, url, headers=headers, json=json, params=params
            )
        except httpx.HTTPError as exc:
            raise ExternalError("github request failed", reason=type(exc).__name__) from exc
        if response.status_code in (401, 403):
            raise UnauthorizedError("github rejected the access token")
        if response.status_code >= 400:
            raise ExternalError("github request failed", status_code=response.status_code)
        body: dict[str, Any] = response.json()
        return body
