import asyncio
import time
from pathlib import Path
from typing import NoReturn

import httpx
import jwt

from app.core.exceptions import ExternalError, ForbiddenError, GitOpsConflictError, NotFoundError

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
        self, installation_id: int, repository_name: str | None, contents: str = "read"
    ) -> str:
        """contents 권한 설치 토큰(1시간)을 만든다. repository_name 이 있으면 그 레포로만 좁힌다.

        repository_name 은 owner 를 뺀 저장소 이름이다. 설치 계정의 저장소만 지정할 수 있다.
        """
        body: dict[str, object] = {"permissions": {"contents": contents}}
        if repository_name is not None:
            body["repositories"] = [repository_name]
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

    # --- Git Database API (GitOps 저장소). 커밋은 새로 만들고 브랜치는 fast-forward 만 한다.

    async def find_subtree_sha(
        self, token: str, full_name: str, commit_sha: str, path: str
    ) -> str | None:
        """commit_sha 시점 path 디렉터리의 tree SHA. 없으면 None."""
        commit = await self._send("GET", f"/repos/{full_name}/git/commits/{commit_sha}", token)
        tree_sha: str = commit.json()["tree"]["sha"]
        for name in path.split("/"):
            tree = await self._send("GET", f"/repos/{full_name}/git/trees/{tree_sha}", token)
            entry = next(
                (e for e in tree.json()["tree"] if e["path"] == name and e["type"] == "tree"),
                None,
            )
            if entry is None:
                return None
            tree_sha = entry["sha"]
        return tree_sha

    async def create_tree(self, token: str, full_name: str, files: dict[str, str]) -> str:
        """files(이름 → 내용)만 담은 새 tree 를 만든다."""
        entries = [
            {"path": name, "mode": "100644", "type": "blob", "content": content}
            for name, content in sorted(files.items())
        ]
        response = await self._send(
            "POST", f"/repos/{full_name}/git/trees", token, json={"tree": entries}
        )
        sha: str = response.json()["sha"]
        return sha

    async def create_commit(
        self, token: str, full_name: str, parent_sha: str, path: str, tree_sha: str, message: str
    ) -> str:
        """parent 트리에서 path 디렉터리만 tree_sha 로 바꾼 커밋. 브랜치는 움직이지 않는다."""
        return await self._commit_tree_entry(
            token, full_name, parent_sha, message, {"path": path, "sha": tree_sha}
        )

    async def create_delete_commit(
        self, token: str, full_name: str, parent_sha: str, path: str, message: str
    ) -> str:
        """parent 트리에서 path 디렉터리를 지운 커밋. 브랜치는 움직이지 않는다.

        path 가 parent 에 없으면 GitHub 이 422 를 주어 NotFoundError 가 된다. 지운 뒤 비는 부모
        디렉터리는 GitHub 이 함께 정리한다.
        """
        return await self._commit_tree_entry(
            token, full_name, parent_sha, message, {"path": path, "sha": None}
        )

    async def _commit_tree_entry(
        self,
        token: str,
        full_name: str,
        parent_sha: str,
        message: str,
        entry: dict[str, str | None],
    ) -> str:
        """parent 트리의 디렉터리 항목 하나(sha 가 None 이면 삭제)를 바꾼 커밋을 만든다."""
        parent = await self._send("GET", f"/repos/{full_name}/git/commits/{parent_sha}", token)
        root = await self._send(
            "POST",
            f"/repos/{full_name}/git/trees",
            token,
            json={
                "base_tree": parent.json()["tree"]["sha"],
                "tree": [{**entry, "mode": "040000", "type": "tree"}],
            },
        )
        commit = await self._send(
            "POST",
            f"/repos/{full_name}/git/commits",
            token,
            json={"message": message, "tree": root.json()["sha"], "parents": [parent_sha]},
        )
        sha: str = commit.json()["sha"]
        return sha

    async def update_branch(self, token: str, full_name: str, branch: str, commit_sha: str) -> None:
        """fast-forward 만 한다. 브랜치가 그새 움직였으면 GitOpsConflictError."""
        response = await self._send(
            "PATCH",
            f"/repos/{full_name}/git/refs/heads/{branch}",
            token,
            json={"sha": commit_sha, "force": False},
            allowed_statuses=(409, 422),
        )
        if response.is_error:
            raise GitOpsConflictError(
                "branch moved", branch=branch, status_code=response.status_code
            )

    async def contains(self, token: str, full_name: str, commit_sha: str, head_sha: str) -> bool:
        """head_sha 가 commit_sha 와 같거나 그 이후 커밋인가."""
        if commit_sha == head_sha:
            return True
        response = await self._send(
            "GET", f"/repos/{full_name}/compare/{commit_sha}...{head_sha}", token
        )
        return response.json()["status"] in ("ahead", "identical")

    async def _send(
        self,
        method: str,
        path: str,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        allowed_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        merged_headers = {**(_auth(token) if token else {}), **(headers or {})}
        try:
            response = await self._http.request(
                method, path, headers=merged_headers, json=json, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise ExternalError("github request failed", path=path) from exc
        if response.is_error and response.status_code not in allowed_statuses:
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
