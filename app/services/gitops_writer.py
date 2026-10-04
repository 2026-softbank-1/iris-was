"""GitOps 저장소 main 에 커밋을 올리는 공용 절차. Deploy Worker 만 쓴다.

커밋은 새로 만들고 main 은 fast-forward 만 한다. 외부에 쓰기 전에 커밋 SHA 를 먼저 기록해,
Worker 가 죽어도 기록된 커밋으로 이어서 처리한다(중복 커밋 없음).
"""

import logging
import time
from collections.abc import Awaitable, Callable

from app.clients.github_client import GitHubClient
from app.core.exceptions import ExternalError, GitOpsConflictError

logger = logging.getLogger(__name__)

GITOPS_BRANCH = "main"
MAX_PUSH_ATTEMPTS = 5
# 설치 토큰은 1시간 유효하다. 만료 직전 토큰을 쓰지 않게 일찍 갱신한다.
_TOKEN_TTL_SECONDS = 50 * 60


class GitOpsWriter:
    def __init__(self, github: GitHubClient, installation_id: int, repository: str) -> None:
        self._github = github
        self._installation_id = installation_id
        self._repository = repository
        self._token: tuple[str, float] | None = None

    @property
    def repository(self) -> str:
        """`{owner}/{repo}`."""
        return self._repository

    async def token(self) -> str:
        if self._token is None or time.monotonic() >= self._token[1]:
            token = await self._github.create_installation_token(
                self._installation_id, None, contents="write"
            )
            self._token = (token, time.monotonic() + _TOKEN_TTL_SECONDS)
        return self._token[0]

    async def push(
        self,
        token: str,
        recorded_sha: str | None,
        create: Callable[[str], Awaitable[str]],
        record: Callable[[str], Awaitable[None]],
    ) -> None:
        """커밋을 main 에 fast-forward 한다. 기록된 커밋이 이미 main 에 있으면 그대로 끝낸다.

        `create(head_sha)` 는 HEAD 위에 커밋을 만들고, `record(commit_sha)` 는 브랜치를 옮기기 전에
        그 SHA 를 기록한다. 브랜치가 그새 움직였으면 새 HEAD 위에 커밋을 다시 만든다.
        """
        commit_sha = recorded_sha
        if commit_sha is not None:
            head_sha = await self._github.get_branch_sha(token, self._repository, GITOPS_BRANCH)
            if await self._github.contains(token, self._repository, commit_sha, head_sha):
                return
        for _ in range(MAX_PUSH_ATTEMPTS):
            if commit_sha is None:
                head_sha = await self._github.get_branch_sha(token, self._repository, GITOPS_BRANCH)
                commit_sha = await create(head_sha)
                await record(commit_sha)
            try:
                await self._github.update_branch(token, self._repository, GITOPS_BRANCH, commit_sha)
                return
            except GitOpsConflictError:
                logger.info("gitops branch moved, recommitting", extra={"action": "push"})
                commit_sha = None
        raise ExternalError("gitops branch kept moving", attempts=MAX_PUSH_ATTEMPTS)
