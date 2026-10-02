import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.clients.source_repository_client import SourceRepositoryClient
from app.core.exceptions import ExternalError
from app.core.worker_exceptions import GitOpsConflictError

logger = logging.getLogger(__name__)


class GithubGitOpsClient:
    """Limited Git Database API writes; recorded commits are pushed by fast-forward only."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: SourceRepositoryClient,
        installation_id: int,
        repository: str,
        branch: str = "main",
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._installation_id = installation_id
        self._repository = repository
        self._branch = branch
        self._base = api_base_url.rstrip("/")
        self._token: tuple[str, float] | None = None

    async def get_head_sha(self) -> str:
        body = await self._send("GET", f"/git/ref/heads/{quote(self._branch, safe='/')}")
        return str(body["object"]["sha"])

    async def find_subtree_sha(self, commit_sha: str, path: str) -> str | None:
        commit = await self._send("GET", f"/git/commits/{commit_sha}")
        tree_sha = str(commit["tree"]["sha"])
        for name in path.split("/"):
            tree = await self._send("GET", f"/git/trees/{tree_sha}")
            entry = next(
                (
                    entry
                    for entry in tree["tree"]
                    if entry["path"] == name and entry["type"] == "tree"
                ),
                None,
            )
            if entry is None:
                return None
            tree_sha = str(entry["sha"])
        return tree_sha

    async def find_values(self, commit_sha: str, path: str) -> dict[str, Any] | None:
        body = await self._send(
            "GET", f"/contents/{path}/values.yaml?ref={commit_sha}", allow_not_found=True
        )
        if body is None:
            return None
        import base64
        import json

        try:
            value = json.loads(base64.b64decode(body["content"], validate=False))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (KeyError, ValueError, TypeError):
            raise ExternalError("gitops values are not a supported manifest") from None

    async def create_values_commit(
        self, parent_sha: str, path: str, values: str, message: str
    ) -> str:
        tree = await self._send(
            "POST",
            "/git/trees",
            json={
                "tree": [
                    {"path": "values.yaml", "mode": "100644", "type": "blob", "content": values}
                ]
            },
        )
        return await self.create_subtree_commit(parent_sha, path, str(tree["sha"]), message)

    async def create_subtree_commit(
        self, parent_sha: str, path: str, tree_sha: str, message: str
    ) -> str:
        parent = await self._send("GET", f"/git/commits/{parent_sha}")
        root = await self._send(
            "POST",
            "/git/trees",
            json={
                "base_tree": parent["tree"]["sha"],
                "tree": [{"path": path, "mode": "040000", "type": "tree", "sha": tree_sha}],
            },
        )
        commit = await self._send(
            "POST",
            "/git/commits",
            json={"message": message, "tree": root["sha"], "parents": [parent_sha]},
        )
        return str(commit["sha"])

    async def update_branch(self, commit_sha: str) -> None:
        await self._send(
            "PATCH",
            f"/git/refs/heads/{quote(self._branch, safe='/')}",
            json={"sha": commit_sha, "force": False},
            conflict_on_422=True,
        )

    async def contains(self, commit_sha: str, head_sha: str | None) -> bool:
        if head_sha is None:
            return False
        if commit_sha == head_sha:
            return True
        body = await self._send("GET", f"/compare/{commit_sha}...{head_sha}")
        return body["status"] in {"ahead", "identical"}

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        allow_not_found: bool = False,
        conflict_on_422: bool = False,
    ) -> Any:
        if self._token is None or time.monotonic() >= self._token[1]:
            token = await self._credentials.create_installation_token(self._installation_id)
            self._token = (token.token, time.monotonic() + 50 * 60)
        try:
            response = await self._http.request(
                method,
                f"{self._base}/repos/{self._repository}{path}",
                headers={
                    "Authorization": f"Bearer {self._token[0]}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=json,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ExternalError("gitops request failed") from None
        if allow_not_found and response.status_code == 404:
            return None
        if conflict_on_422 and response.status_code in {409, 422}:
            raise GitOpsConflictError("gitops branch moved")
        if response.is_error:
            raise ExternalError("gitops request failed", status_code=response.status_code)
        try:
            return response.json()
        except ValueError:
            raise ExternalError("gitops returned an invalid response") from None
