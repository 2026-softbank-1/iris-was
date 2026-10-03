"""실패한 배포의 AI 진단: 로그·소스를 모아 에이전트에 보내고 결과를 저장한다.

에이전트 호출은 모델을 두 번까지 부르므로 최대 두 분 남짓 걸린다. 그동안 DB 연결을 잡지 않도록
진행 중 행을 먼저 커밋하고, 읽기 트랜잭션도 외부 호출 전에 닫는다. 진단 결과로 배포 요청의
상태는 바꾸지 않는다.
"""

import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.aws_clients import PRESIGNED_URL_SECONDS, SnapshotUrlClient
from app.clients.diagnosis_agent_client import DiagnosisAgentClient
from app.core.exceptions import (
    AppError,
    DeploymentNotFailedError,
    DeploymentRequestNotFoundError,
    DiagnosisAgentError,
    DiagnosisInProgressError,
    DiagnosisLogsUnavailableError,
    DiagnosisNotFoundError,
    NotConfiguredError,
    ServiceNotFoundError,
)
from app.enums import DeploymentStatus, FailureCode
from app.models.base import now_utc
from app.models.build import Build
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_request import DeploymentRequest
from app.models.service import Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_diagnosis_repository import DeploymentDiagnosisRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.service_repository import ServiceRepository
from app.schemas.diagnosis import (
    AgentDiagnoseData,
    AgentDiagnosisResult,
    AgentLogEvent,
    AgentLogRange,
    AgentSource,
    FailedStage,
)
from app.services.observability_service import ObservabilityService
from app.services.upload_source import is_upload_source_sha

logger = logging.getLogger(__name__)

# 실패한 요청만 진단한다. 롤백된 요청과 사람이 봐야 하는 요청의 원인도 로그에 남아 있다.
DIAGNOSABLE_STATUSES = frozenset(
    {
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.MANUAL_INTERVENTION,
    }
)
# 에이전트 대기 한도(150초)와 여유. 이보다 오래 RUNNING 이면 서버가 죽어 남은 행으로 본다.
STALE_AFTER = timedelta(minutes=4)
STALE_ERROR_CODE = "DIAGNOSIS_ABANDONED"
# 서버가 실패한 배포를 자동으로 진단하는 범위. 끝난 지 이 시간이 지난 실패는 자동으로 되살리지
# 않는다(배포 직후 옛 실패를 한꺼번에 돌려 모델 비용을 쓰지 않도록). 에이전트는 동시에 두 건만
# 받으므로 자동 진단은 한 번에 하나만, 사용자가 시작한 진단이 돌고 있어도 기다린다.
AUTO_MAX_AGE = timedelta(minutes=10)
AUTO_MAX_RUNNING = 1
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
BUILD_LOG_SOURCE_ID = "codebuild"

LOG_FETCH_LIMIT = 1000
# 에이전트는 마스킹한 로그+메타데이터가 16KiB 를 넘으면 거절한다(INPUT_TOO_LARGE). 로그 한 줄마다
# 메타데이터가 붙으므로 줄 수를 바이트로 환산해 맞추고, 그래도 거절하면 절반으로 줄여 다시 보낸다.
LOG_BUDGETS_BYTES = (12_000, 6_000)
_BYTES_PER_LINE_OVERHEAD = 200
_MAX_LINE_CHARS = 2_000
# 실패 시각 뒤 조금까지 본다. 재시도·롤백 로그가 이어서 남기 때문이다.
_LOG_WINDOW_MARGIN = timedelta(minutes=5)
_MAX_LOG_AGE = timedelta(days=7)
# 빌드가 올린 스냅샷은 하루 뒤 지워진다. 그 전까지만 소스를 함께 보낸다.
_SNAPSHOT_RETENTION = timedelta(hours=23)

_BUILD_FAILED_PHASE_PATTERN = re.compile(r"Phase complete: \w+ State: FAILED")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

LogStage = Literal["build", "runtime"]


@dataclass(frozen=True)
class DiagnosisLogLine:
    timestamp: datetime
    source_id: str
    text: str


@dataclass(frozen=True)
class CollectedLogs:
    """진단에 보낼 후보 로그. 오래된 줄부터 순서대로다."""

    stage: LogStage
    lines: list[DiagnosisLogLine]
    window: tuple[datetime, datetime]
    # 모으는 단계에서 이미 일부가 빠졌다(조회 한도·저장된 끝부분만 있음).
    is_partial: bool


# 실패 사유로 실패한 단계를 가린다. DEPLOY_FAILED 는 Sync·readiness 실패라 앱이 못 뜬 경우가 많아
# 앱 로그(runtime)가 단서다. 시간 초과·인프라 오류는 클러스터 쪽(deploy)이다.
_FAILED_STAGE_BY_FAILURE_CODE: dict[FailureCode, FailedStage] = {
    FailureCode.SOURCE_NOT_ACCESSIBLE: "build",
    FailureCode.SOURCE_REF_NOT_FOUND: "build",
    FailureCode.SOURCE_TOO_LARGE: "build",
    FailureCode.SOURCE_INVALID: "build",
    FailureCode.BUILD_CONFIG_REQUIRED: "build",
    FailureCode.BUILD_FAILED: "build",
    FailureCode.BUILD_TIMED_OUT: "build",
    FailureCode.BUILD_INFRA_ERROR: "build",
    FailureCode.DEPLOY_FAILED: "runtime",
    FailureCode.DEPLOY_TIMED_OUT: "deploy",
    FailureCode.DEPLOY_INFRA_ERROR: "deploy",
}


@dataclass(frozen=True)
class StartedDiagnosis:
    diagnosis: DeploymentDiagnosis
    # False 면 새로 진단하지 않고 저장된 성공 결과를 그대로 돌려준 것이다.
    is_started: bool


@dataclass(frozen=True)
class AutomaticDiagnosis:
    """서버가 자동으로 시작한 진단. `run_diagnosis` 에 그대로 넘겨 이어 간다."""

    owner_id: int
    service_id: int
    deployment_request_id: int
    diagnosis_id: int


# 요청이 끝난 뒤 진단을 이어 갈 때 쓴다. 요청의 DB 세션은 이미 닫혔으므로 새 세션으로 서비스를 연다.
DiagnosisServiceOpener = Callable[[], AbstractAsyncContextManager["DiagnosisService"]]


class DiagnosisService:
    def __init__(
        self,
        session: AsyncSession,
        service_repository: ServiceRepository,
        deployment_request_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        diagnosis_repository: DeploymentDiagnosisRepository,
        observability_service: ObservabilityService,
        agent_client: DiagnosisAgentClient | None,
        snapshot_client: SnapshotUrlClient | None,
    ) -> None:
        self._session = session
        self._service_repository = service_repository
        self._deployment_request_repository = deployment_request_repository
        self._build_repository = build_repository
        self._diagnosis_repository = diagnosis_repository
        self._observability_service = observability_service
        self._agent_client = agent_client
        self._snapshot_client = snapshot_client

    async def diagnose(
        self, owner_id: int, service_id: int, deployment_request_id: int, *, refresh: bool = False
    ) -> DeploymentDiagnosis:
        """진단을 시작하고 끝날 때까지 기다린다. 실패하면 기록한 뒤 그 오류를 던진다."""
        started = await self.start_diagnosis(
            owner_id, service_id, deployment_request_id, refresh=refresh
        )
        if not started.is_started:
            return started.diagnosis
        return await self.run_diagnosis(
            owner_id, service_id, deployment_request_id, started.diagnosis.id
        )

    async def start_diagnosis(
        self, owner_id: int, service_id: int, deployment_request_id: int, *, refresh: bool = False
    ) -> StartedDiagnosis:
        """실패한 배포의 진단을 시작한다. 진행 중(RUNNING) 행을 커밋해 돌려주고 모델은 안 부른다.

        이미 성공한 진단이 있으면 새로 만들지 않고 그 결과를 돌려준다(refresh 면 새로 진단한다).
        같은 배포 요청을 진단하는 중이면 DiagnosisInProgressError, 에이전트가 설정되지 않았으면
        NotConfiguredError 다. 실제 진단은 `run_diagnosis` 가 한다.
        """
        self._require_agent_client()
        service = await self._get_owned_service(owner_id, service_id)
        request = await self._get_deployment_request(service.id, deployment_request_id)
        if request.status not in DIAGNOSABLE_STATUSES:
            raise DeploymentNotFailedError(
                "only failed deployments can be diagnosed",
                deployment_request_id=request.id,
                deployment_status=request.status,
            )
        if not refresh:
            existing = (
                await self._diagnosis_repository.find_latest_succeeded_by_deployment_request_id(
                    request.id
                )
            )
            if existing is not None:
                return StartedDiagnosis(existing, is_started=False)

        await self._diagnosis_repository.fail_stale_running(
            request.id, now_utc() - STALE_AFTER, STALE_ERROR_CODE
        )
        diagnosis = await self._diagnosis_repository.add_running_if_absent(request.id, owner_id)
        if diagnosis is None:
            raise DiagnosisInProgressError(
                "diagnosis is already running", deployment_request_id=request.id
            )
        await self._session.commit()
        return StartedDiagnosis(diagnosis, is_started=True)

    async def start_next_automatic_diagnosis(self) -> AutomaticDiagnosis | None:
        """실패가 확정됐는데 아직 진단하지 않은 배포 하나의 진단을 사용자 없이 시작한다.

        진행 중(RUNNING) 행(`requested_by` 는 비어 있다)을 커밋해 돌려주고 모델은 안 부른다.
        시작할 배포가 없거나, 이미 진단이 돌고 있거나, 다른 서버가 먼저 시작했으면 None 이다.
        소유자는 서비스를 가진 프로젝트의 소유자다. 실제 진단은 `run_diagnosis` 가 한다.
        """
        self._require_agent_client()
        now = now_utc()
        stale_before = now - STALE_AFTER
        running = await self._diagnosis_repository.count_running_since(stale_before)
        if running >= AUTO_MAX_RUNNING:
            return None
        request = await self._diagnosis_repository.find_next_auto_start_candidate(
            now - AUTO_MAX_AGE, stale_before, DIAGNOSABLE_STATUSES
        )
        if request is None:
            return None
        owner_id = await self._service_repository.find_owner_id_by_id(request.service_id)
        if owner_id is None:
            return None

        await self._diagnosis_repository.fail_stale_running(
            request.id, stale_before, STALE_ERROR_CODE
        )
        diagnosis = await self._diagnosis_repository.add_running_if_absent(request.id, None)
        if diagnosis is None:
            return None
        await self._session.commit()
        logger.info(
            "diagnosis started automatically",
            extra={
                "action": "start_next_automatic_diagnosis",
                "service_id": request.service_id,
                "deployment_request_id": request.id,
                "diagnosis_id": diagnosis.id,
            },
        )
        return AutomaticDiagnosis(owner_id, request.service_id, request.id, diagnosis.id)

    async def run_diagnosis(
        self, owner_id: int, service_id: int, deployment_request_id: int, diagnosis_id: int
    ) -> DeploymentDiagnosis:
        """`start_diagnosis` 가 만든 진행 중 행의 진단을 실행해 결과(또는 실패 사유)를 저장한다.

        실패하면 사유를 행에 남긴 뒤 그 오류를 다시 던진다.
        """
        agent_client = self._require_agent_client()
        service = await self._get_owned_service(owner_id, service_id)
        request = await self._get_deployment_request(service.id, deployment_request_id)
        diagnosis = await self._diagnosis_repository.get_by_id(diagnosis_id)
        try:
            result = await self._run(owner_id, service, request, agent_client)
        except AppError as exc:
            await self._finish_failed(diagnosis, service.id, _failure_reason(exc))
            raise
        except Exception:
            # 예상 못 한 오류(DB 등). 트랜잭션이 깨졌을 수 있어 되돌리고 행을 다시 읽어 기록한다.
            await self._session.rollback()
            diagnosis = await self._diagnosis_repository.get_by_id(diagnosis_id)
            await self._finish_failed(diagnosis, service.id, INTERNAL_ERROR_CODE)
            raise
        diagnosis.succeed(result)
        await self._session.commit()
        logger.info(
            "diagnosis succeeded",
            extra={
                "action": "run_diagnosis",
                "service_id": service.id,
                "deployment_request_id": request.id,
                "diagnosis_id": diagnosis.id,
            },
        )
        return diagnosis

    def _require_agent_client(self) -> DiagnosisAgentClient:
        if self._agent_client is None:
            raise NotConfiguredError(
                "diagnosis agent is not configured",
                setting="DIAGNOSIS_AGENT_URL, DIAGNOSIS_AGENT_API_KEY",
            )
        return self._agent_client

    async def _finish_failed(
        self, diagnosis: DeploymentDiagnosis, service_id: int, error_code: str
    ) -> None:
        diagnosis.fail(error_code)
        await self._session.commit()
        logger.warning(
            "diagnosis failed",
            extra={
                "action": "run_diagnosis",
                "service_id": service_id,
                "deployment_request_id": diagnosis.deployment_request_id,
                "diagnosis_id": diagnosis.id,
                "error_code": error_code,
            },
        )

    async def get_diagnosis(
        self, owner_id: int, service_id: int, deployment_request_id: int
    ) -> DeploymentDiagnosis:
        """배포 요청의 가장 최근 진단. 진단한 적이 없으면 DiagnosisNotFoundError 다."""
        service = await self._get_owned_service(owner_id, service_id)
        request = await self._get_deployment_request(service.id, deployment_request_id)
        diagnosis = await self._diagnosis_repository.find_latest_by_deployment_request_id(
            request.id
        )
        if diagnosis is None:
            raise DiagnosisNotFoundError(
                "diagnosis not found", deployment_request_id=deployment_request_id
            )
        return diagnosis

    async def _run(
        self,
        owner_id: int,
        service: Service,
        request: DeploymentRequest,
        agent_client: DiagnosisAgentClient,
    ) -> dict[str, Any]:
        build = await self._build_repository.find_by_deployment_request_id(request.id)
        snapshot_build_id = await self._find_snapshot_build_id(request, build)
        failed_stage = _failed_stage(request)
        if failed_stage == "build":
            # 빌드 단계 실패는 Build Worker 가 저장해 둔 빌드 로그로 진단한다. 앱은 뜬 적이 없다.
            collected = _collect_build_logs(build, request.id)
            # 읽기 트랜잭션을 닫는다. 이후 에이전트 호출 동안 DB 연결을 잡지 않는다.
            await self._session.commit()
        else:
            collected = await self._collect_runtime_logs(owner_id, service, request)
        source = await self._build_source(service, request, snapshot_build_id)

        raw: dict[str, Any] | None = None
        for index, budget in enumerate(LOG_BUDGETS_BYTES):
            events, is_trimmed = _select_log_events(collected.lines, collected.stage, budget)
            if not events:
                raise _logs_unavailable(collected.stage, request.id)
            data = AgentDiagnoseData(
                project_id=service.project_id,
                service_id=service.id,
                deployment_id=request.id,
                attempt_id=build.attempt if build is not None else None,
                deployment_status=request.status,
                failed_stage=failed_stage,
                log_range=AgentLogRange(
                    from_=collected.window[0],
                    to=collected.window[1],
                    is_complete=not collected.is_partial and not is_trimmed,
                ),
                logs=events,
                source=source,
            )
            try:
                raw = await agent_client.diagnose(
                    data.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
            except DiagnosisAgentError as exc:
                if exc.agent_code == "EMPTY_LOGS":
                    raise _logs_unavailable(collected.stage, request.id) from exc
                is_last_budget = index + 1 == len(LOG_BUDGETS_BYTES)
                if exc.agent_code == "INPUT_TOO_LARGE" and not is_last_budget:
                    continue
                raise
            break
        assert raw is not None
        return _validate_result(raw)

    async def _collect_runtime_logs(
        self, owner_id: int, service: Service, request: DeploymentRequest
    ) -> CollectedLogs:
        target_ids = (
            await self._service_repository.search_target_ids_by_service_ids([service.id])
        ).get(service.id, [])
        namespace = (
            await self._observability_service.get_scope(owner_id, service.id, target_ids[0])
            if target_ids
            else None
        )
        # 읽기 트랜잭션을 닫는다. 이후 Loki·에이전트 호출 동안 DB 연결을 잡지 않는다.
        await self._session.commit()

        window = _log_window(request, now_utc())
        if namespace is None or window is None:
            raise _logs_unavailable("runtime", request.id)
        entries = await self._observability_service.search_logs(
            target_ids[0],
            namespace,
            _to_ns(window[0]),
            _to_ns(window[1]),
            LOG_FETCH_LIMIT,
            "",
        )
        return CollectedLogs(
            stage="runtime",
            lines=[
                DiagnosisLogLine(_from_ns(entry.timestamp_ns), entry.pod or "app", entry.message)
                for entry in entries
            ],
            window=window,
            is_partial=len(entries) >= LOG_FETCH_LIMIT,
        )

    async def _find_snapshot_build_id(
        self, request: DeploymentRequest, build: Build | None
    ) -> int | None:
        """소스 스냅샷을 올린 빌드. 롤백·재시작은 빌드하지 않으므로 원본 요청의 빌드를 따라간다."""
        if self._snapshot_client is None:
            return None
        candidate = build
        if (
            candidate is None or candidate.codebuild_build_id is None
        ) and request.source_deployment_request_id is not None:
            candidate = await self._build_repository.find_by_deployment_request_id(
                request.source_deployment_request_id
            )
        # CodeBuild 를 시작했다면 스냅샷은 이미 올라가 있다.
        if candidate is None or candidate.codebuild_build_id is None:
            return None
        if now_utc() - candidate.created_at > _SNAPSHOT_RETENTION:
            return None
        return candidate.id

    async def _build_source(
        self, service: Service, request: DeploymentRequest, snapshot_build_id: int | None
    ) -> AgentSource | None:
        if self._snapshot_client is None or snapshot_build_id is None:
            return None
        return AgentSource(
            download_url=await self._snapshot_client.presign_snapshot(snapshot_build_id),
            expires_at=now_utc() + timedelta(seconds=PRESIGNED_URL_SECONDS),
            commit_sha=request.source_sha
            if _COMMIT_SHA_PATTERN.match(request.source_sha)
            else None,
            # 업로드 스냅샷은 올린 폴더가 루트라서 서비스의 root_directory 를 적용하지 않는다.
            root_directory="."
            if is_upload_source_sha(request.source_sha)
            else service.root_directory or ".",
        )

    async def _get_owned_service(self, owner_id: int, service_id: int) -> Service:
        service = await self._service_repository.find_by_id_and_owner_id(service_id, owner_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        return service

    async def _get_deployment_request(
        self, service_id: int, deployment_request_id: int
    ) -> DeploymentRequest:
        request = await self._deployment_request_repository.find_by_id_and_service_id(
            deployment_request_id, service_id
        )
        if request is None:
            raise DeploymentRequestNotFoundError(
                "deployment request not found",
                service_id=service_id,
                deployment_request_id=deployment_request_id,
            )
        return request


def _failed_stage(request: DeploymentRequest) -> FailedStage | None:
    if request.failure_code is None:
        return None
    return _FAILED_STAGE_BY_FAILURE_CODE.get(request.failure_code)


def _log_window(request: DeploymentRequest, now: datetime) -> tuple[datetime, datetime] | None:
    """요청이 만들어진 때부터 마지막 상태 변경 직후까지. 조회 가능한 기간을 벗어나면 None 이다."""
    start = max(request.created_at, now - _MAX_LOG_AGE + timedelta(minutes=1))
    end = min(request.updated_at + _LOG_WINDOW_MARGIN, now)
    return (start, end) if start < end else None


def _to_ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1e9)


def _from_ns(timestamp_ns: str) -> datetime:
    return _EPOCH + timedelta(microseconds=int(timestamp_ns) // 1000)


def _logs_unavailable(stage: LogStage, deployment_request_id: int) -> DiagnosisLogsUnavailableError:
    return DiagnosisLogsUnavailableError(
        f"no {stage} logs to diagnose", deployment_request_id=deployment_request_id
    )


def _collect_build_logs(build: Build | None, deployment_request_id: int) -> CollectedLogs:
    """`builds.log_tail` 에서 빌드 로그를 꺼낸다. 없거나 읽을 수 없으면 진단하지 않는다."""
    tail = build.log_tail if build is not None else None
    raw_entries = tail.get("entries") if isinstance(tail, dict) else None
    lines: list[DiagnosisLogLine] = []
    for entry in raw_entries if isinstance(raw_entries, list) else []:
        try:
            lines.append(
                DiagnosisLogLine(
                    timestamp=datetime.fromisoformat(entry["timestamp"]),
                    source_id=BUILD_LOG_SOURCE_ID,
                    text=str(entry["message"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    lines = _drop_after_build_failure(lines)
    if not lines:
        raise _logs_unavailable("build", deployment_request_id)
    return CollectedLogs(
        stage="build",
        lines=lines,
        window=(lines[0].timestamp, lines[-1].timestamp),
        is_partial=bool(tail.get("is_truncated")) if isinstance(tail, dict) else False,
    )


def _drop_after_build_failure(lines: list[DiagnosisLogLine]) -> list[DiagnosisLogLine]:
    """마지막 실패 표시줄(`Phase complete: BUILD State: FAILED`) 뒤를 버린다.

    CodeBuild 는 단계가 실패해도 뒤 단계(POST_BUILD·UPLOAD_ARTIFACTS)를 이어서 돌리고
    실패한 스크립트를 통째로 다시 출력한다. 이 잡음이 최근 줄부터 고르는 예산을 채워 정작
    오류 출력이 밀려난다. 표시줄이 없으면(시간 초과 등) 그대로 둔다.
    """
    for index in range(len(lines) - 1, -1, -1):
        if _BUILD_FAILED_PHASE_PATTERN.search(lines[index].text):
            return lines[: index + 1]
    return lines


def _select_log_events(
    lines: list[DiagnosisLogLine], stage: LogStage, budget_bytes: int
) -> tuple[list[AgentLogEvent], bool]:
    """오래된 순으로 정렬된 로그에서 가장 최근 것부터 예산 안에 드는 만큼 고른다.

    실패 원인은 대개 끝에 있다. 잘렸으면 두 번째 값이 True 다. 빈 줄만 있는 로그는 건너뛴다.
    """
    selected: list[tuple[DiagnosisLogLine, str]] = []
    used = 0
    is_trimmed = False
    for line in reversed(lines):
        text = line.text[:_MAX_LINE_CHARS]
        if not text.strip():
            continue
        cost = sum(
            len(part.encode("utf-8")) + _BYTES_PER_LINE_OVERHEAD for part in text.splitlines()
        )
        if used + cost > budget_bytes:
            is_trimmed = True
            break
        selected.append((line, text))
        used += cost
    selected.reverse()
    events = [
        AgentLogEvent(
            id=f"log-{index:04d}",
            timestamp=line.timestamp,
            stage=stage,
            source_id=line.source_id,
            # CodeBuild 로그는 stdout·stderr 가 한 줄기로 섞여 있다.
            stream="combined" if stage == "build" else None,
            sequence=index,
            text=text,
        )
        for index, (line, text) in enumerate(selected, start=1)
    ]
    return events, is_trimmed


def _validate_result(raw: dict[str, Any]) -> dict[str, Any]:
    """에이전트 응답이 쓸 수 있는 진단인지 확인한다. 가짜 진단을 성공으로 저장하지 않는다."""
    try:
        result = AgentDiagnosisResult.model_validate(raw)
    except ValidationError as exc:
        raise DiagnosisAgentError(
            "diagnosis agent returned an unexpected result", agent_code="INVALID_RESPONSE"
        ) from exc
    if result.job_status != "succeeded" or result.analysis is None:
        error_code = result.error.get("code") if result.error else None
        raise DiagnosisAgentError(
            "diagnosis agent did not finish the diagnosis",
            agent_code=error_code if isinstance(error_code, str) else result.job_status,
        )
    return raw


def _failure_reason(exc: AppError) -> str:
    reason = exc.agent_code if isinstance(exc, DiagnosisAgentError) and exc.agent_code else exc.code
    return reason[:64]


async def run_diagnosis_in_background(
    open_service: DiagnosisServiceOpener,
    owner_id: int,
    service_id: int,
    deployment_request_id: int,
    diagnosis_id: int,
) -> None:
    """응답을 보낸 뒤 진단을 끝까지 실행한다. 결과와 실패 사유는 진단 행에 남는다."""
    try:
        async with open_service() as service:
            await service.run_diagnosis(owner_id, service_id, deployment_request_id, diagnosis_id)
    except AppError:
        # run_diagnosis 가 사유를 행에 남기고 로그도 찍었다. 호출자는 응답을 이미 보냈다.
        return
    except Exception:
        logger.exception(
            "diagnosis crashed",
            extra={
                "action": "run_diagnosis_in_background",
                "service_id": service_id,
                "deployment_request_id": deployment_request_id,
                "diagnosis_id": diagnosis_id,
            },
        )
