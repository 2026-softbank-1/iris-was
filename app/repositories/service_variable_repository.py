from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import now_utc
from app.models.service_variable import ServiceVariable


class ServiceVariableRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_service_id_and_key(self, service_id: int, key: str) -> ServiceVariable | None:
        stmt = select(ServiceVariable).where(
            ServiceVariable.service_id == service_id, ServiceVariable.key == key
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def search_by_service_id(self, service_id: int) -> list[ServiceVariable]:
        """서비스의 변수를 키 순으로 돌려준다."""
        stmt = (
            select(ServiceVariable)
            .where(ServiceVariable.service_id == service_id)
            .order_by(ServiceVariable.key)
        )
        return list((await self._session.scalars(stmt)).all())

    async def add_if_absent(
        self, service_id: int, key: str, encrypted_value: str
    ) -> ServiceVariable | None:
        """같은 키가 이미 있으면 만들지 않고 None 이다. 동시에 들어온 요청도 DB 제약이 가른다."""
        stmt = (
            insert(ServiceVariable)
            .values(service_id=service_id, key=key, encrypted_value=encrypted_value)
            .on_conflict_do_nothing()
            .returning(ServiceVariable)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def delete(self, variable: ServiceVariable) -> None:
        await self._session.delete(variable)

    async def replace_all(self, service_id: int, encrypted_values: dict[str, str]) -> None:
        """서비스의 변수를 주어진 집합으로 맞춘다. 없는 키는 지우고 있는 키는 값을 덮어쓴다."""
        await self._session.execute(
            delete(ServiceVariable).where(
                ServiceVariable.service_id == service_id,
                ServiceVariable.key.not_in(list(encrypted_values)),
            )
        )
        if not encrypted_values:
            return
        stmt = insert(ServiceVariable).values(
            [
                {"service_id": service_id, "key": key, "encrypted_value": value}
                for key, value in encrypted_values.items()
            ]
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["service_id", "key"],
                set_={"encrypted_value": stmt.excluded.encrypted_value, "updated_at": now_utc()},
            )
        )
