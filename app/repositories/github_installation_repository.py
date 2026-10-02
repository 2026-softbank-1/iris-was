from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.user import GithubInstallation, UserGithubInstallation


class GithubInstallationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, github_installation_id: int) -> GithubInstallation:
        installation = await self._session.get(GithubInstallation, github_installation_id)
        if installation is None:
            raise NotFoundError(
                "github installation not found", github_installation_id=github_installation_id
            )
        return installation

    async def find_by_installation_id(self, installation_id: int) -> GithubInstallation | None:
        stmt = select(GithubInstallation).where(
            GithubInstallation.installation_id == installation_id
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_by_user_id(self, user_id: int) -> list[GithubInstallation]:
        stmt = (
            select(GithubInstallation)
            .join(
                UserGithubInstallation,
                UserGithubInstallation.github_installation_id == GithubInstallation.id,
            )
            .where(UserGithubInstallation.user_id == user_id)
            .order_by(GithubInstallation.account_login)
        )
        return list((await self._session.scalars(stmt)).all())

    async def save(self, installation: GithubInstallation) -> GithubInstallation:
        self._session.add(installation)
        await self._session.flush()
        return installation

    async def replace_user_links(self, user_id: int, github_installation_ids: set[int]) -> None:
        """사용자가 접근할 수 있는 설치를 주어진 집합과 같게 맞춘다."""
        await self._session.execute(
            delete(UserGithubInstallation).where(
                UserGithubInstallation.user_id == user_id,
                UserGithubInstallation.github_installation_id.not_in(github_installation_ids),
            )
        )
        if github_installation_ids:
            await self._session.execute(
                insert(UserGithubInstallation)
                .values(
                    [
                        {"user_id": user_id, "github_installation_id": installation_id}
                        for installation_id in github_installation_ids
                    ]
                )
                .on_conflict_do_nothing()
            )

    async def delete_user_links_by_github_installation_id(
        self, github_installation_id: int
    ) -> None:
        """설치가 제거되면 어느 사용자의 목록에도 나오지 않게 연결을 끊는다. 설치 행은 남긴다."""
        await self._session.execute(
            delete(UserGithubInstallation).where(
                UserGithubInstallation.github_installation_id == github_installation_id
            )
        )
