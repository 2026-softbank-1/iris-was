import asyncio
import time
from pathlib import Path
from typing import NoReturn

import httpx
import jwt

from app.core.exceptions import ExternalError, ForbiddenError, NotFoundError

GITHUB_API_URL = "https://api.github.com"


class SourceTooLargeError(Exception):
    """tarball 이 max_bytes 를 넘었다."""


class GitHubClient:
    """GitHub App 으로 인증해 소스를 받는다.

    404·422 는 NotFoundError, 401·403 은 ForbiddenError,
    그 밖의 실패는 ExternalError(재시도) 로 바꾼다.
    """

    def __init__(self, http: httpx.AsyncClient, app_id: int, private_key: str) -> None:
        self._http = http
        self._app_id = app_id
        self._private_key = private_key

    async def create_installation_token(
        self, installation_id: int, repository_id: int | None
    ) -> str:
        """contents:read 설치 토큰(1시간)을 만든다. repository_id 가 있으면 그 레포로만 좁힌다."""
        body: dict[str, object] = {"permissions": {"contents": "read"}}
        if repository_id is not None:
            body["repository_ids"] = [repository_id]
        response = await self._send(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._create_app_jwt()}"},
            json=body,
        )
        token: str = response.json()["token"]
        return token

    async def get_branch_sha(self, token: str, full_name: str, branch: str | None = None) -> str:
        """브랜치 HEAD SHA. branch 가 없으면 default 브랜치다."""
        if branch is None:
            repository = await self._send("GET", f"/repos/{full_name}", token=token)
            branch = repository.json()["default_branch"]
        response = await self._send(
            "GET",
            f"/repos/{full_name}/commits/{branch}",
            token=token,
            headers={"Accept": "application/vnd.github.sha"},
        )
        return response.text.strip()

    async def download_tarball(
        self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int
    ) -> None:
        """sha 의 tarball(.tar.gz)을 dest 에 저장한다. max_bytes 를 넘으면 SourceTooLargeError."""
        request = self._http.build_request(
            "GET", f"/repos/{full_name}/tarball/{sha}", headers=_auth(token)
        )
        try:
            response = await self._http.send(request, stream=True, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ExternalError("github request failed", path=request.url.path) from exc
        try:
            if response.is_error:
                await response.aread()
                _raise_for_status(response)
            file = await asyncio.to_thread(dest.open, "wb")
            try:
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceTooLargeError
                    await asyncio.to_thread(file.write, chunk)
            finally:
                file.close()
        except httpx.HTTPError as exc:
            raise ExternalError("github download failed", path=request.url.path) from exc
        finally:
            await response.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        json: object | None = None,
    ) -> httpx.Response:
        merged_headers = {**(_auth(token) if token else {}), **(headers or {})}
        try:
            response = await self._http.request(
                method, path, headers=merged_headers, json=json, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise ExternalError("github request failed", path=path) from exc
        if response.is_error:
            _raise_for_status(response)
        return response

    def _create_app_jwt(self) -> str:
        now = int(time.time())
        # 서버 시계 차이를 감안해 iat 를 60초 앞당긴다. exp 는 최대 10분이다.
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_status(response: httpx.Response) -> NoReturn:
    fields = {"path": response.request.url.path, "status_code": response.status_code}
    if response.status_code in (404, 422):
        raise NotFoundError("github resource not found", **fields)
    if response.status_code in (401, 403) and response.headers.get("x-ratelimit-remaining") != "0":
        raise ForbiddenError("github access denied", **fields)
    raise ExternalError("github request failed", **fields)
