"""레포 구성 분석(Analysis Gate) 테스트용 인메모리 Repository·대역."""

from itertools import count
from types import SimpleNamespace
from typing import Any

from app.enums import DeploymentTrigger
from app.models.base import now_utc
from app.models.repository_analysis import RepositoryAnalysis
from tests.fakes_project import FakeProjectRepository


class FakeRepositoryAnalysisRepository:
    def __init__(self, projects: FakeProjectRepository) -> None:
        self._projects = projects
        self.analyses: dict[int, RepositoryAnalysis] = {}
        self._ids = count(1)

    async def save(self, analysis: RepositoryAnalysis) -> RepositoryAnalysis:
        if analysis.id is None:
            analysis.id = next(self._ids)
            analysis.created_at = analysis.updated_at = now_utc()
            analysis.attempts = 0
        self.analyses[analysis.id] = analysis
        return analysis

    async def find_by_id_and_project_id(
        self, analysis_id: int, project_id: int, *, for_update: bool = False
    ) -> RepositoryAnalysis | None:
        analysis = self.analyses.get(analysis_id)
        project = self._projects.projects.get(project_id)
        if analysis is None or analysis.project_id != project_id:
            return None
        if project is None or project.is_deleted:
            return None
        return analysis

    async def get_by_id(self, analysis_id: int, *, for_update: bool = False) -> RepositoryAnalysis:
        return self.analyses[analysis_id]


class FakeManualDeploymentService:
    """apply 가 만드는 배포 요청을 기록한다. 같은 키는 처음 만든 요청을 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.requests: dict[str, SimpleNamespace] = {}
        self._ids = count(1)

    async def create_deployment_request(
        self,
        owner_id: int,
        service_id: int,
        *,
        trigger_type: DeploymentTrigger,
        source_sha: str | None = None,
        idempotency_key: str | None = None,
        **_: Any,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "owner_id": owner_id,
                "service_id": service_id,
                "trigger_type": trigger_type,
                "source_sha": source_sha,
                "idempotency_key": idempotency_key,
            }
        )
        key = f"manual:{service_id}:{idempotency_key}"
        if key not in self.requests:
            self.requests[key] = SimpleNamespace(id=next(self._ids), service_id=service_id)
        return self.requests[key]
