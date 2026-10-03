import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import jwt

from app.core.exceptions import ExternalError, ForbiddenError, UnauthorizedError

_APP_JWT_BACKDATE_SECONDS = 60
_APP_JWT_TTL_SECONDS = 540  # GitHub 상한은 10분이다.


@dataclass(frozen=True)
class RepositoryInfo:
    full_name: str
    url: str
    default_branch: str
    is_private: bool


@dataclass(frozen=True)
class BranchInfo:
    name: str
    is_default: bool


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str


@dataclass(frozen=True)
class InstallationToken:
    """저장소용 단기 설치 토큰. 로그에 남기지 않고 신뢰된 호출자에게만 전달한다."""

    token: str = field(repr=False)
    expires_at: datetime


class SourceRepositoryClient(Protocol):
    async def create_installation_token(self, installation_id: int) -> InstallationToken: ...

    async def create_repair_token(
        self, installation_id: int, full_name: str
    ) -> InstallationToken: ...

    async def fetch_repositories(self, installation_id: int) -> list[RepositoryInfo]:
        """설치에 권한이 있는 저장소 전체."""
        ...

    async def find_repository(self, installation_id: int, full_name: str) -> RepositoryInfo | None:
        """설치로 접근할 수 없거나 없는 저장소면 None."""
        ...

    async def fetch_branches(self, installation_id: int, full_name: str) -> list[BranchInfo]: ...

    async def find_branch_head(
        self, installation_id: int, full_name: str, branch: str
    ) -> CommitInfo | None:
        """브랜치가 가리키는 최신 커밋. 브랜치가 없으면 None."""
        ...


class GithubSourceRepositoryClient:
    """GitHub App 설치 토큰으로 저장소를 조회한다."""

    _PAGE_SIZE = 100
    _MAX_PAGES = 10

    def __init__(
        self, http: httpx.AsyncClient, app_id: str, private_key: str, api_base_url: str
    ) -> None:
        self._http = http
        self._app_id = app_id
        # 환경변수에 한 줄로 넣은 PEM 의 `\n` 두 글자를 줄바꿈으로 되돌린다.
        self._private_key = private_key.replace("\\n", "\n")
        self._api_base_url = api_base_url.rstrip("/")

    async def create_installation_token(self, installation_id: int) -> InstallationToken:
        body = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._create_app_jwt(),
        )
        return InstallationToken(
            token=str(body["token"]),
            expires_at=datetime.fromisoformat(str(body["expires_at"])),
        )

    async def create_repair_token(self, installation_id: int, full_name: str) -> InstallationToken:
        """서비스 소스 저장소 하나에 Contents·PR write 만 부여한다."""
        response = await self._send(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._create_app_jwt(),
            params=None,
            json={
                "repositories": [full_name.split("/", 1)[1]],
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
        )
        if (
            response.status_code in {403, 422}
            and response.headers.get("x-ratelimit-remaining") != "0"
        ):
            raise ForbiddenError("github app requires Contents and Pull requests write")
        self._raise_for_status(response)
        body = response.json()
        try:
            permissions = body.get("permissions", {})
            if any(permissions.get(p) != "write" for p in ("contents", "pull_requests")):
                raise ForbiddenError("github app requires Contents and Pull requests write")
            value = body["token"]
            expires = datetime.fromisoformat(body["expires_at"])
            if not isinstance(value, str) or not value or expires.tzinfo is None:
                raise ValueError
            if expires <= datetime.now(UTC):
                raise ValueError
            return InstallationToken(value, expires)
        except (KeyError, ValueError, TypeError):
            raise ExternalError("invalid github repair token response") from None

    async def fetch_repositories(self, installation_id: int) -> list[RepositoryInfo]:
        token = (await self.create_installation_token(installation_id)).token
        repositories: list[RepositoryInfo] = []
        for page in range(1, self._MAX_PAGES + 1):
            body = await self._request(
                "GET",
                "/installation/repositories",
                token=token,
                params={"per_page": self._PAGE_SIZE, "page": page},
            )
            items = body.get("repositories", [])
            repositories.extend(self._to_repository_info(item) for item in items)
            if len(items) < self._PAGE_SIZE:
                break
        return repositories

    async def find_repository(self, installation_id: int, full_name: str) -> RepositoryInfo | None:
        token = (await self.create_installation_token(installation_id)).token
        body = await self._request("GET", f"/repos/{full_name}", token=token, allow_not_found=True)
        return None if body is None else self._to_repository_info(body)

    async def fetch_branches(self, installation_id: int, full_name: str) -> list[BranchInfo]:
        token = (await self.create_installation_token(installation_id)).token
        repository = await self._request("GET", f"/repos/{full_name}", token=token)
        default_branch = repository["default_branch"]
        branches: list[BranchInfo] = []
        for page in range(1, self._MAX_PAGES + 1):
            items = await self._request_list(
                f"/repos/{full_name}/branches",
                token=token,
                params={"per_page": self._PAGE_SIZE, "page": page},
            )
            branches.extend(
                BranchInfo(item["name"], item["name"] == default_branch) for item in items
            )
            if len(items) < self._PAGE_SIZE:
                break
        return branches

    async def find_branch_head(
        self, installation_id: int, full_name: str, branch: str
    ) -> CommitInfo | None:
        token = (await self.create_installation_token(installation_id)).token
        body = await self._request(
            "GET",
            f"/repos/{full_name}/branches/{quote(branch, safe='/')}",
            token=token,
            allow_not_found=True,
        )
        if body is None:
            return None
        commit = body["commit"]
        return CommitInfo(sha=str(commit["sha"]), message=str(commit["commit"]["message"]))

    def _create_app_jwt(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._app_id,
            "iat": now - _APP_JWT_BACKDATE_SECONDS,
            "exp": now + _APP_JWT_TTL_SECONDS,
        }
        try:
            return jwt.encode(claims, self._private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError):
            # 키 내용은 예외 메시지에 들어갈 수 있어 원인을 잇지 않는다.
            raise ExternalError("github app private key is invalid") from None

    @staticmethod
    def _to_repository_info(item: dict[str, Any]) -> RepositoryInfo:
        return RepositoryInfo(
            full_name=str(item["full_name"]),
            url=str(item["html_url"]),
            default_branch=str(item.get("default_branch") or "main"),
            is_private=bool(item.get("private", False)),
        )

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, int] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        response = await self._send(method, path, token, params)
        if allow_not_found and response.status_code == 404:
            return None
        self._raise_for_status(response)
        return response.json()

    async def _request_list(
        self, path: str, token: str, params: dict[str, int] | None = None
    ) -> list[dict[str, Any]]:
        response = await self._send("GET", path, token, params)
        self._raise_for_status(response)
        items: list[dict[str, Any]] = response.json()
        return items

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, int] | None,
        *,
        json: object | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                f"{self._api_base_url}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            raise ExternalError("github request failed", reason=type(exc).__name__) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise UnauthorizedError("github rejected the app credentials")
        if response.status_code >= 400:
            raise ExternalError("github request failed", status_code=response.status_code)
