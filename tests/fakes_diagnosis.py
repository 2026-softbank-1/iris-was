"""AI 진단 테스트가 함께 쓰는 조립 도우미."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from itertools import count
from typing import Any

from pydantic import HttpUrl

from app.clients.observability_client import LogEntry
from app.core.exceptions import DiagnosisNotFoundError
from app.enums import DeploymentStatus, DeploymentTrigger, DiagnosisStatus, Environment, FailureCode
from app.models.base import now_utc
from app.models.build import Build
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.services.diagnosis_service import DiagnosisService, DiagnosisServiceOpener
from app.services.observability_service import ObservabilityService
from tests.fakes_deployment import OWNER, DeploymentSetup

SOURCE_SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


class FakeDiagnosisRepository:
    def __init__(self) -> None:
        self.rows: list[DeploymentDiagnosis] = []
        self._ids = count(1)

    def seed(
        self,
        deployment_request_id: int,
        status: DiagnosisStatus,
        *,
        age: timedelta = timedelta(0),
        result: dict[str, Any] | None = None,
    ) -> DeploymentDiagnosis:
        created_at = now_utc() - age
        row = DeploymentDiagnosis(
            id=next(self._ids),
            deployment_request_id=deployment_request_id,
            status=status,
            result=result,
            created_at=created_at,
            updated_at=created_at,
        )
        self.rows.append(row)
        return row

    async def get_by_id(self, diagnosis_id: int) -> DeploymentDiagnosis:
        row = next((r for r in self.rows if r.id == diagnosis_id), None)
        if row is None:
            raise DiagnosisNotFoundError("diagnosis not found", diagnosis_id=diagnosis_id)
        return row

    async def add_running_if_absent(
        self, deployment_request_id: int, requested_by: int | None
    ) -> DeploymentDiagnosis | None:
        if any(
            r.deployment_request_id == deployment_request_id and r.status == DiagnosisStatus.RUNNING
            for r in self.rows
        ):
            return None
        row = self.seed(deployment_request_id, DiagnosisStatus.RUNNING)
        row.requested_by = requested_by
        return row

    async def find_latest_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentDiagnosis | None:
        rows = [r for r in self.rows if r.deployment_request_id == deployment_request_id]
        return max(rows, key=lambda r: (r.created_at, r.id), default=None)

    async def find_latest_succeeded_by_deployment_request_id(
        self, deployment_request_id: int
    ) -> DeploymentDiagnosis | None:
        rows = [
            r
            for r in self.rows
            if r.deployment_request_id == deployment_request_id
            and r.status == DiagnosisStatus.SUCCEEDED
        ]
        return max(rows, key=lambda r: (r.created_at, r.id), default=None)

    async def fail_stale_running(
        self, deployment_request_id: int, started_before: Any, error_code: str
    ) -> None:
        for row in self.rows:
            if (
                row.deployment_request_id == deployment_request_id
                and row.status == DiagnosisStatus.RUNNING
                and row.created_at < started_before
            ):
                row.fail(error_code)


class FakeObservabilityClient:
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []
        self.calls: list[dict[str, Any]] = []

    async def search_logs(
        self,
        base_url: str,
        namespace: str,
        start_ns: int,
        end_ns: int,
        limit: int,
        search: str,
        direction: str = "backward",
    ) -> list[LogEntry]:
        self.calls.append(
            {"namespace": namespace, "start_ns": start_ns, "end_ns": end_ns, "limit": limit}
        )
        return self.entries[-limit:]


class FakeDiagnosisAgentClient:
    """호출한 요청 본문을 기록하고, 준비한 응답(또는 예외)을 차례로 돌려준다."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any] | Exception] = [valid_agent_result()]

    async def diagnose(self, data: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(data)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class FakeSnapshotUrlClient:
    def __init__(self) -> None:
        self.build_ids: list[int] = []

    async def presign_snapshot(self, build_id: int) -> str:
        self.build_ids.append(build_id)
        return f"https://bucket.s3.ap-northeast-2.amazonaws.com/snapshots/{build_id}.tar.gz?sig=x"


_LOG_BASE_NS = int((now_utc() - timedelta(minutes=8)).timestamp() * 1e9)


def make_log(index: int, message: str | None = None, pod: str = "app-0") -> LogEntry:
    """8분 전부터 index 초 뒤에 찍힌 로그. 요청이 만들어진 10분 전 이후라 조회 범위 안이다."""
    return LogEntry(str(_LOG_BASE_NS + index * 10**9), message or f"line {index}", pod, "app")


def make_log_tail(messages: list[str], *, is_truncated: bool = False) -> dict[str, Any]:
    """Build Worker 가 `builds.log_tail` 에 남기는 모양. 8분 전부터 1초 간격으로 찍힌 로그다."""
    base = now_utc() - timedelta(minutes=8)
    return {
        "entries": [
            {
                "timestamp": (base + timedelta(seconds=i)).isoformat().replace("+00:00", "Z"),
                "message": message,
            }
            for i, message in enumerate(messages)
        ],
        "is_truncated": is_truncated,
    }


def valid_agent_result() -> dict[str, Any]:
    """diagnosis-result.v3 중 이 서버가 읽는 부분의 실제 모양."""
    return {
        "schema_version": "diagnosis-result.v3",
        "diagnosis_id": "diag-1",
        "job_status": "succeeded",
        "error": None,
        "analysis": {
            "analysis_status": "diagnosed",
            "summary": "DATABASE_URL 이 없어 앱이 시작하지 못했다.",
            "observations": [
                {
                    "id": "O1",
                    "kind": "failure",
                    "text": "필수 설정 누락",
                    "evidence_ids": ["EV000001"],
                }
            ],
            "hypotheses": [
                {
                    "id": "H1",
                    "category": "configuration",
                    "support_level": "direct",
                    "statement": "DATABASE_URL 환경변수가 설정되지 않았다.",
                    "observation_ids": ["O1"],
                    "evidence_ids": ["EV000001"],
                    "counter_evidence_ids": [],
                    "uncertainty": "다른 변수도 빠졌을 수 있다.",
                }
            ],
            "next_checks": [
                {
                    "id": "C1",
                    "target": "서비스 변수",
                    "method": "DATABASE_URL 이 등록됐는지 본다.",
                    "purpose": "누락 확인",
                    "hypothesis_ids": ["H1"],
                }
            ],
            "missing_information": [],
            "limitations": ["소스를 보지 못했다."],
            "remediation": {
                "status": "proposed",
                "reason": "원인이 로그에 직접 나온다.",
                "plans": [
                    {
                        "id": "R1",
                        "title": "DATABASE_URL 변수 추가",
                        "hypothesis_ids": ["H1"],
                        "evidence_ids": ["EV000001"],
                        "apply_when": ["DATABASE_URL 이 서비스 변수에 없을 때"],
                        "changes": [
                            {
                                "kind": "configuration",
                                "target": "서비스 변수",
                                "target_known": True,
                                "instruction": "DATABASE_URL 을 추가한다.",
                                "language": "dotenv",
                                "snippet_kind": "template",
                                "snippet": "DATABASE_URL={{DATABASE_URL}}",
                                "placeholders": [
                                    {"name": "DATABASE_URL", "description": "DB 접속 문자열"}
                                ],
                            }
                        ],
                        "verification": [
                            {"instruction": "다시 배포한다.", "expected_result": "앱이 시작한다."}
                        ],
                        "rollback": ["변수를 삭제한다."],
                        "risks": ["잘못된 값이면 연결에 실패한다."],
                    }
                ],
            },
        },
        "evidence": [
            {
                "id": "EV000001",
                "chunk_id": "c1",
                "source_id": "app-0",
                "stage": "runtime",
                "stream": "unknown",
                "chunk_line": 1,
                "source_line": None,
                "text": "ERROR Missing required configuration: DATABASE_URL",
                "timestamp": "2026-10-03T01:00:00Z",
                "event_line": 1,
            }
        ],
        "input_limitations": [],
        "source_analysis": {
            "status": "not_needed",
            "reason": "로그만으로 충분하다.",
            "commit_sha": None,
            "findings": [],
            "evidence": [],
            "limitations": [],
            "error": None,
        },
        "execution": {"elapsed_ms": 1234},
    }


class DiagnosisSetup(DeploymentSetup):
    """OWNER 의 서비스 하나(타깃 1)와 실패한 배포 요청을 둔 인메모리 조립."""

    def __init__(self) -> None:
        super().__init__()
        self.diagnoses = FakeDiagnosisRepository()
        self.loki = FakeObservabilityClient()
        self.agent = FakeDiagnosisAgentClient()
        self.snapshots = FakeSnapshotUrlClient()

    async def build(self) -> "DiagnosisSetup":
        await super().build()
        await self.services.replace_targets(self.service.id, {1})
        self.loki.entries = [make_log(i) for i in range(1, 4)]
        return self

    def add_request(
        self,
        status: DeploymentStatus = DeploymentStatus.FAILED,
        failure_code: FailureCode | None = FailureCode.DEPLOY_FAILED,
        *,
        source_deployment_request_id: int | None = None,
        source_sha: str = SOURCE_SHA,
    ) -> DeploymentRequest:
        now = now_utc()
        request = DeploymentRequest(
            id=len(self.requests.requests) + 1,
            service_id=self.service.id,
            environment=Environment.PROD,
            source_sha=source_sha,
            trigger_type=DeploymentTrigger.MANUAL,
            idempotency_key=f"key-{len(self.requests.requests) + 1}",
            status=status,
            failure_code=failure_code,
            source_deployment_request_id=source_deployment_request_id,
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=6),
        )
        self.requests.requests.append(request)
        return request

    def add_build(
        self,
        request: DeploymentRequest,
        *,
        codebuild_build_id: str | None = "cb-1",
        attempt: int = 2,
        log_tail: dict[str, Any] | None = None,
    ) -> Build:
        build = Build(
            deployment_request_id=request.id,
            codebuild_build_id=codebuild_build_id,
            attempt=attempt,
            log_tail=log_tail,
            created_at=now_utc() - timedelta(minutes=10),
            updated_at=now_utc() - timedelta(minutes=10),
        )
        build.id = len(self.builds.builds) + 1
        self.builds.builds.append(build)
        return build

    def diagnosis_service(self, *, agent: bool = True, snapshots: bool = False) -> DiagnosisService:
        observability = ObservabilityService(
            self.services,  # type: ignore[arg-type]
            self.loki,  # type: ignore[arg-type]
            HttpUrl("http://loki.internal"),
            None,
        )
        return DiagnosisService(
            self.session,  # type: ignore[arg-type]
            self.services,  # type: ignore[arg-type]
            self.requests,  # type: ignore[arg-type]
            self.builds,  # type: ignore[arg-type]
            self.diagnoses,  # type: ignore[arg-type]
            observability,
            self.agent if agent else None,
            self.snapshots if snapshots else None,
        )

    def diagnosis_service_opener(
        self, *, agent: bool = True, snapshots: bool = False
    ) -> DiagnosisServiceOpener:
        """백그라운드 실행이 서비스를 여는 방식. 같은 인메모리 저장소를 쓰는 서비스를 열어 준다."""

        @asynccontextmanager
        async def open_service() -> AsyncIterator[DiagnosisService]:
            yield self.diagnosis_service(agent=agent, snapshots=snapshots)

        return open_service


__all__ = ["OWNER", "DiagnosisSetup", "make_log", "make_log_tail", "valid_agent_result"]
