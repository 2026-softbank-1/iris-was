from collections.abc import Collection, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database_init_script import DatabaseInitScript


class DatabaseInitScriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_all_if_absent(self, contents: Mapping[str, bytes]) -> None:
        """sha256 → 내용. 이미 있는 sha256 은 같은 내용이라 그대로 둔다."""
        if not contents:
            return
        await self._session.execute(
            insert(DatabaseInitScript)
            .values(
                [
                    {"sha256": sha256, "size_bytes": len(content), "content": content}
                    for sha256, content in sorted(contents.items())
                ]
            )
            .on_conflict_do_nothing(index_elements=["sha256"])
        )

    async def search_by_sha256s(self, sha256s: Collection[str]) -> list[DatabaseInitScript]:
        if not sha256s:
            return []
        stmt = select(DatabaseInitScript).where(DatabaseInitScript.sha256.in_(set(sha256s)))
        return list((await self._session.scalars(stmt)).all())

    async def search_existing_sha256s(self, sha256s: Collection[str]) -> set[str]:
        """내용은 읽지 않고 저장된 sha256 만 본다."""
        if not sha256s:
            return set()
        stmt = select(DatabaseInitScript.sha256).where(DatabaseInitScript.sha256.in_(set(sha256s)))
        return set((await self._session.scalars(stmt)).all())
