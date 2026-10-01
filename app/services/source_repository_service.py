import asyncio
import logging
from dataclasses import dataclass

from app.clients.source_repository_client import (
    BranchInfo,
    CommitInfo,
    InstallationToken,
    RepositoryInfo,
    SourceRepositoryClient,
)
from app.core.exceptions import RepositoryNotAccessibleError
from app.models.user import GithubInstallation
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.services.repository_url import parse_repository_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepositoryCandidate:
    """사용자가 서비스에 연결할 수 있는 저장소와, 그 저장소에 접근하는 설치."""

    installation_id: int
    full_name: str
    url: str
    default_branch: str
    is_private: bool

    @classmethod
    def from_info(cls, installation_id: int, info: RepositoryInfo) -> "RepositoryCandidate":
        return cls(installation_id, info.full_name, info.url, info.default_branch, info.is_private)


class SourceRepositoryService:
    """GitHub 저장소 조회. 사용자가 접근 권한을 준(설치한) 저장소만 다룬다."""

    def __init__(
        self,
        installation_repository: GithubInstallationRepository,
        source_repository_client: SourceRepositoryClient,
    ) -> None:
        self._installation_repository = installation_repository
        self._source_repository_client = source_repository_client

    async def search_installations(self, user_id: int) -> list[GithubInstallation]:
        return await self._installation_repository.search_by_user_id(user_id)

    async def search_repositories(
        self, user_id: int, query: str | None = None, installation_id: int | None = None
    ) -> list[RepositoryCandidate]:
        installations = await self._installation_repository.search_by_user_id(user_id)
        if installation_id is not None:
            installations = [i for i in installations if i.installation_id == installation_id]

        fetched = await asyncio.gather(
            *(
                self._source_repository_client.fetch_repositories(i.installation_id)
                for i in installations
            )
        )
        needle = (query or "").strip().lower()
        candidates = [
            RepositoryCandidate.from_info(installation.installation_id, info)
            for installation, infos in zip(installations, fetched, strict=True)
            for info in infos
            if needle in info.full_name.lower()
        ]
        return sorted(candidates, key=lambda candidate: candidate.full_name.lower())

    async def get_repository(self, user_id: int, full_name: str) -> RepositoryCandidate:
        """`owner/name` 저장소가 사용자의 설치로 접근 가능한지 확인하고 돌려준다."""
        installation = await self._get_installation_for(user_id, full_name)
        info = await self._source_repository_client.find_repository(
            installation.installation_id, full_name
        )
        if info is None:
            raise self._not_accessible(full_name)
        return RepositoryCandidate.from_info(installation.installation_id, info)

    async def resolve_repository(self, user_id: int, url: str) -> RepositoryCandidate:
        owner, name = parse_repository_url(url)
        return await self.get_repository(user_id, f"{owner}/{name}")

    async def search_branches(self, user_id: int, full_name: str) -> list[BranchInfo]:
        repository = await self.get_repository(user_id, full_name)
        return await self._source_repository_client.fetch_branches(
            repository.installation_id, repository.full_name
        )

    async def find_branch_head(
        self, user_id: int, full_name: str, branch: str
    ) -> CommitInfo | None:
        """사용자가 접근할 수 있는 저장소의 브랜치 최신 커밋. 브랜치가 없으면 None."""
        repository = await self.get_repository(user_id, full_name)
        return await self._source_repository_client.find_branch_head(
            repository.installation_id, repository.full_name, branch
        )

    async def create_clone_token(self, installation_id: int) -> InstallationToken:
        """빌드 쪽이 저장소를 clone 할 때 쓰는 단기 토큰."""
        return await self._source_repository_client.create_installation_token(installation_id)

    async def _get_installation_for(self, user_id: int, full_name: str) -> GithubInstallation:
        owner = full_name.split("/", 1)[0].lower()
        installations = await self._installation_repository.search_by_user_id(user_id)
        # 설치는 계정(사용자·조직) 단위라서 저장소 소유자와 같은 계정의 설치를 쓴다.
        installation = next((i for i in installations if i.account_login.lower() == owner), None)
        if installation is None:
            raise self._not_accessible(full_name)
        return installation

    @staticmethod
    def _not_accessible(full_name: str) -> RepositoryNotAccessibleError:
        return RepositoryNotAccessibleError(
            "repository is not accessible; install the github app and grant access",
            full_name=full_name,
        )
