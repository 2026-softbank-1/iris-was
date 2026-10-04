"""서비스 환경변수 테스트용 인메모리 Repository."""

from collections.abc import Collection
from itertools import count
from typing import Any

from cryptography.fernet import Fernet

from app.core.crypto import VariableCipher
from app.models.base import now_utc
from app.models.project import Project
from app.models.service import Service
from app.models.service_variable import ServiceVariable
from app.services.variable_service import VariableService
from tests.fakes import FakeSession
from tests.fakes_project import FakeProjectRepository, FakeServiceRepository


class FakeServiceVariableRepository:
    def __init__(self) -> None:
        self.variables: list[ServiceVariable] = []
        self._ids = count(1)

    async def find_by_service_id_and_key(self, service_id: int, key: str) -> ServiceVariable | None:
        return next(
            (v for v in self.variables if v.service_id == service_id and v.key == key), None
        )

    async def search_by_service_id(self, service_id: int) -> list[ServiceVariable]:
        return sorted(
            (v for v in self.variables if v.service_id == service_id), key=lambda v: v.key
        )

    async def add_if_absent(
        self,
        service_id: int,
        key: str,
        encrypted_value: str | None,
        reference: dict[str, Any] | None = None,
    ) -> ServiceVariable | None:
        if await self.find_by_service_id_and_key(service_id, key) is not None:
            return None
        variable = ServiceVariable(
            service_id=service_id, key=key, encrypted_value=encrypted_value, reference=reference
        )
        variable.id = next(self._ids)
        variable.created_at = variable.updated_at = now_utc()
        self.variables.append(variable)
        return variable

    async def delete(self, variable: ServiceVariable) -> None:
        self.variables.remove(variable)

    async def replace_all(
        self,
        service_id: int,
        encrypted_values: dict[str, str],
        keep_keys: Collection[str] = (),
    ) -> None:
        self.variables = [
            v
            for v in self.variables
            if v.service_id != service_id or v.key in encrypted_values or v.key in keep_keys
        ]
        for key, value in encrypted_values.items():
            existing = await self.find_by_service_id_and_key(service_id, key)
            if existing is None:
                await self.add_if_absent(service_id, key, value)
            else:
                existing.encrypted_value = value
                existing.reference = None


OWNER = 1
STRANGER = 2


class VariableSetup:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.projects = FakeProjectRepository()
        self.services = FakeServiceRepository(self.projects)
        self.variables = FakeServiceVariableRepository()
        self.cipher = VariableCipher(Fernet.generate_key().decode())
        self.service: Service

    async def build(self) -> "VariableSetup":
        project = await self.projects.save(Project(name="p", owner_id=OWNER))
        self.service = await self.services.save(
            Service(
                project_id=project.id,
                name="web",
                source_repository_url="https://github.com/iris-org/web",
                github_installation_id=1,
                source_branch="main",
                is_auto_deploy=True,
            )
        )
        return self

    def variable_service(self) -> VariableService:
        return VariableService(
            self.session,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.variables,  # type: ignore[arg-type]
            self.cipher,
        )
