"""단위 테스트용 가짜 Repository·Client. DB·네트워크 없이 Service 를 검증한다."""

from datetime import UTC, datetime
from itertools import count

from app.clients.oauth_client import InstallationInfo, OAuthUser
from app.clients.source_repository_client import (
    BranchInfo,
    CommitInfo,
    InstallationToken,
    RepositoryInfo,
)
from app.models.cli_login_session import CliLoginSession
from app.models.user import GithubInstallation, User


class FakeSession:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class FakeUserRepository:
    def __init__(self, users: list[User] | None = None) -> None:
        self.users: dict[int, User] = {user.id: user for user in users or []}
        self._ids = count(max(self.users, default=0) + 1)

    async def find_by_id(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    async def find_by_github_id(self, github_id: int) -> User | None:
        return next((u for u in self.users.values() if u.github_id == github_id), None)

    async def save(self, user: User) -> User:
        if getattr(user, "id", None) is None:
            user.id = next(self._ids)
        self.users[user.id] = user
        return user


class FakeCliLoginSessionRepository:
    def __init__(self) -> None:
        self.sessions: dict[int, CliLoginSession] = {}
        self.for_update_lookups = 0
        self._ids = count(1)

    async def find_by_public_id(self, public_id: str) -> CliLoginSession | None:
        return next((s for s in self.sessions.values() if s.public_id == public_id), None)

    async def find_by_public_id_for_update(self, public_id: str) -> CliLoginSession | None:
        self.for_update_lookups += 1
        return await self.find_by_public_id(public_id)

    async def save(self, cli_session: CliLoginSession) -> CliLoginSession:
        if getattr(cli_session, "id", None) is None:
            cli_session.id = next(self._ids)
        self.sessions[cli_session.id] = cli_session
        return cli_session

    async def delete_expired_before(self, cutoff: datetime) -> None:
        self.sessions = {i: s for i, s in self.sessions.items() if s.expires_at >= cutoff}


class FakeGithubInstallationRepository:
    def __init__(self) -> None:
        self.installations: dict[int, GithubInstallation] = {}
        self.links: dict[int, set[int]] = {}
        self._ids = count(1)

    async def find_by_installation_id(self, installation_id: int) -> GithubInstallation | None:
        return next(
            (i for i in self.installations.values() if i.installation_id == installation_id), None
        )

    async def search_by_user_id(self, user_id: int) -> list[GithubInstallation]:
        return [self.installations[i] for i in sorted(self.links.get(user_id, set()))]

    async def save(self, installation: GithubInstallation) -> GithubInstallation:
        if getattr(installation, "id", None) is None:
            installation.id = next(self._ids)
        self.installations[installation.id] = installation
        return installation

    async def replace_user_links(self, user_id: int, github_installation_ids: set[int]) -> None:
        self.links[user_id] = set(github_installation_ids)

    async def delete_user_links_by_github_installation_id(
        self, github_installation_id: int
    ) -> None:
        for linked in self.links.values():
            linked.discard(github_installation_id)


class FakeOAuthClient:
    def __init__(
        self,
        user: OAuthUser | None = None,
        installations: list[InstallationInfo] | None = None,
    ) -> None:
        self.user = user or OAuthUser(github_id=1001, login="octocat", avatar_url="http://a/1")
        self.installations = installations or []
        self.exchanged_codes: list[str] = []

    def build_authorization_url(self, state: str) -> str:
        return f"https://github.test/authorize?state={state}"

    def build_installation_url(self, app_slug: str, state: str) -> str:
        return f"https://github.test/apps/{app_slug}/installations/new?state={state}"

    async def exchange_code(self, code: str) -> str:
        self.exchanged_codes.append(code)
        return "user-token"

    async def fetch_user(self, access_token: str) -> OAuthUser:
        return self.user

    async def fetch_installations(self, access_token: str) -> list[InstallationInfo]:
        return self.installations


class FakeSourceRepositoryClient:
    """installation_id → 저장소 목록.

    브랜치는 `branches[full_name]`, 브랜치 최신 커밋은 `heads[(full_name, branch)]` 로 지정한다.
    """

    def __init__(self, repositories_by_installation: dict[int, list[RepositoryInfo]]) -> None:
        self.repositories_by_installation = repositories_by_installation
        self.branches: dict[str, list[BranchInfo]] = {}
        self.heads: dict[tuple[str, str], CommitInfo] = {}
        self.token_requests: list[int] = []

    async def create_installation_token(self, installation_id: int) -> InstallationToken:
        self.token_requests.append(installation_id)
        return InstallationToken("ghs_fake", datetime(2030, 1, 1, tzinfo=UTC))

    async def fetch_repositories(self, installation_id: int) -> list[RepositoryInfo]:
        return self.repositories_by_installation.get(installation_id, [])

    async def find_repository(self, installation_id: int, full_name: str) -> RepositoryInfo | None:
        return next(
            (
                r
                for r in self.repositories_by_installation.get(installation_id, [])
                if r.full_name.lower() == full_name.lower()
            ),
            None,
        )

    async def fetch_branches(self, installation_id: int, full_name: str) -> list[BranchInfo]:
        return self.branches.get(full_name, [])

    async def find_branch_head(
        self, installation_id: int, full_name: str, branch: str
    ) -> CommitInfo | None:
        return self.heads.get((full_name, branch))


def make_installation(
    id_: int, installation_id: int, account_login: str, account_type: str = "Organization"
) -> GithubInstallation:
    installation = GithubInstallation(
        installation_id=installation_id, account_login=account_login, account_type=account_type
    )
    installation.id = id_
    return installation


def make_repository(full_name: str, is_private: bool = True) -> RepositoryInfo:
    return RepositoryInfo(full_name, f"https://github.com/{full_name}", "main", is_private)
