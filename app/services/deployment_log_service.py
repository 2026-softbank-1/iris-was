from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.clients.aws_clients import BuildLogReader, LogLine
from app.clients.observability_client import LogEntry, NetworkLogEntry, StatusClass
from app.core.exceptions import InvalidInputError, NotConfiguredError
from app.enums import ACTIVE_DEPLOYMENT_STATUSES, BuildStatus, DeploymentStatus, DeploymentTrigger
from app.models.base import now_utc
from app.models.build import Build
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.services.deployment_history_service import DeploymentDetail, DeploymentHistoryService
from app.services.observability_service import ObservabilityService

# 롤백·재시작 사슬을 따라가는 최대 단계. 사슬이 이보다 길면 빌드 로그가 없는 것으로 본다.
MAX_BUILD_SOURCE_HOPS = 20
MAX_RANGE = timedelta(days=7)
_NO_BUILD_TRIGGERS = (DeploymentTrigger.ROLLBACK, DeploymentTrigger.RESTART)


@dataclass(frozen=True)
class BuildLogScope:
    # None 이면 읽을 CodeBuild 로그가 없다(빌드 전·빌드 실패 전 단계 등).
    log_stream: str | None
    # 로그를 만든 빌드가 속한 배포. 롤백·재시작은 원본 배포다.
    logged_deployment_id: int | None
    build_status: BuildStatus | None
    is_build_finished: bool
    # Build Worker 가 실패한 빌드에 남긴 로그 끝부분(builds.log_tail). CloudWatch 를 읽을 수 없는
    # 환경에서 이것을 대신 보여 준다.
    stored_tail: list[LogLine] = field(default_factory=list)
    is_tail_truncated: bool = False


@dataclass(frozen=True)
class BuildLogPage:
    entries: list[LogLine]
    next_cursor: str | None
    is_complete: bool
    # 저장된 끝부분만 보여 준 것이라 앞부분이 빠졌다.
    is_partial: bool = False


@dataclass(frozen=True)
class LogScope:
    namespace: str
    target_id: int | None
    # 배포 로그에서만 쓴다. 비어 있으면 배포 로그를 조회하지 않는다.
    release_ids: list[int]
    start: datetime | None
    end: datetime | None

    @property
    def query(self) -> tuple[int, datetime, datetime] | None:
        """백엔드에 물을 (타깃, 시작, 끝). 하나라도 없으면 조회할 것이 없다."""
        if self.target_id is None or self.start is None or self.end is None:
            return None
        return self.target_id, self.start, self.end


def _to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1e9)


def _read_stored_tail(build: Build) -> tuple[list[LogLine], bool]:
    """`builds.log_tail`({"entries": [{"timestamp", "message"}], "is_truncated"})을 줄로 푼다.

    형식이 다른 항목은 건너뛴다. 이 값은 Build Worker 가 쓰지만 조회가 그 때문에 깨지면 안 된다.
    """
    tail = build.log_tail or {}
    entries = tail.get("entries")
    lines: list[LogLine] = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            timestamp = datetime.fromisoformat(entry["timestamp"])
            lines.append(LogLine(int(timestamp.timestamp() * 1000), str(entry["message"])))
        except (KeyError, ValueError, TypeError):
            continue
    return lines, bool(tail.get("is_truncated", False))


class DeploymentLogService:
    """배포 요청 하나의 빌드 로그·배포(런타임) 로그·네트워크 로그를 조회한다.

    DB 를 읽는 `get_*_scope` 와 외부 백엔드를 읽는 `search_*` 를 나눈다. 라우터가 둘 사이에서
    DB 세션을 닫아 느린 외부 호출 동안 연결을 쥐지 않게 한다.
    """

    def __init__(
        self,
        deployment_history_service: DeploymentHistoryService,
        deployment_request_repository: DeploymentRequestRepository,
        build_repository: BuildRepository,
        observability_service: ObservabilityService,
        build_log_reader: BuildLogReader | None,
        build_log_group: str | None,
    ) -> None:
        self._deployment_history_service = deployment_history_service
        self._deployment_request_repository = deployment_request_repository
        self._build_repository = build_repository
        self._observability_service = observability_service
        self._build_log_reader = build_log_reader
        self._build_log_group = build_log_group

    async def get_build_log_scope(
        self, owner_id: int, service_id: int, deployment_request_id: int
    ) -> BuildLogScope:
        detail = await self._deployment_history_service.get_deployment_request(
            owner_id, service_id, deployment_request_id
        )
        request, build = detail.deployment_request, detail.build
        hops = 0
        # 롤백·재시작은 빌드를 새로 하지 않고 원본 빌드를 복사한다. 로그는 실제로 빌드한 배포에
        # 있다.
        while (
            build is not None
            and build.codebuild_build_id is None
            and request.trigger_type in _NO_BUILD_TRIGGERS
            and request.source_deployment_request_id is not None
            and hops < MAX_BUILD_SOURCE_HOPS
        ):
            source = await self._deployment_request_repository.find_by_id_and_service_id(
                request.source_deployment_request_id, request.service_id
            )
            if source is None:
                build = None
                break
            request = source
            build = await self._build_repository.find_by_deployment_request_id(source.id)
            hops += 1
        if build is None or build.codebuild_build_id is None:
            is_finished = (
                build.is_finished
                if build is not None
                else request.status not in ACTIVE_DEPLOYMENT_STATUSES
            )
            return BuildLogScope(None, None, build.status if build else None, is_finished)
        # CodeBuild build id 는 `{project}:{uuid}` 이고 로그 스트림 이름은 uuid 다.
        log_stream = build.codebuild_build_id.split(":", 1)[-1]
        stored_tail, is_tail_truncated = _read_stored_tail(build)
        return BuildLogScope(
            log_stream, request.id, build.status, build.is_finished, stored_tail, is_tail_truncated
        )

    async def search_build_logs(
        self, scope: BuildLogScope, cursor: str | None, limit: int
    ) -> BuildLogPage:
        if scope.log_stream is None:
            return BuildLogPage([], cursor, scope.is_build_finished)
        if self._build_log_reader is None or self._build_log_group is None:
            if scope.stored_tail:
                # CloudWatch 를 읽을 수 없으면 Build Worker 가 남긴 끝부분만 보여 준다.
                # 남긴 때는 빌드가 끝난 뒤라 더 읽을 것이 없다.
                return BuildLogPage(
                    scope.stored_tail, None, is_complete=True, is_partial=scope.is_tail_truncated
                )
            raise NotConfiguredError("build logs are not configured", setting="BUILD_LOG_GROUP")
        chunk = await self._build_log_reader.read_events(
            self._build_log_group, scope.log_stream, limit, cursor
        )
        # 끝난 빌드에서 이번에 읽은 줄이 없으면 모두 전달한 것이다.
        return BuildLogPage(
            chunk.lines, chunk.next_token or cursor, scope.is_build_finished and not chunk.lines
        )

    async def get_deploy_log_scope(
        self,
        owner_id: int,
        service_id: int,
        deployment_request_id: int,
        target_id: int | None,
        start: datetime | None,
        end: datetime | None,
    ) -> LogScope:
        """이 배포의 release 로 거를 범위. 기본 구간은 배포를 시작한 때부터 교체될 때까지다."""
        detail = await self._deployment_history_service.get_deployment_request(
            owner_id, service_id, deployment_request_id
        )
        target_id = self._pick_target(detail, target_id)
        release_ids = [
            release.id
            for release in detail.releases
            if target_id is None or release.target_id == target_id
        ]
        namespace = ObservabilityService.build_namespace(detail.service.id)
        default_start = self._first_entered_at(detail, DeploymentStatus.DEPLOYING)
        start, end = self._resolve_range(detail, default_start, start, end)
        if not release_ids:
            # release 가 없으면 로그도 없다. 조회하지 않은 구간은 알리지 않는다.
            return LogScope(namespace, None, [], None, None)
        if target_id is not None:
            # 로그를 어디서 읽을지(Loki·Argo CD)는 DB 세션이 열려 있을 때 정한다.
            await self._observability_service.get_target_kind(target_id)
        return LogScope(namespace, target_id, release_ids, start, end)

    async def search_deploy_logs(self, scope: LogScope, limit: int, search: str) -> list[LogEntry]:
        query = scope.query
        if query is None or not scope.release_ids:
            return []
        target_id, start, end = query
        return await self._observability_service.search_logs(
            target_id,
            scope.namespace,
            _to_ns(start),
            _to_ns(end),
            limit,
            search,
            scope.release_ids,
        )

    async def get_network_log_scope(
        self,
        owner_id: int,
        service_id: int,
        deployment_request_id: int,
        target_id: int | None,
        start: datetime | None,
        end: datetime | None,
    ) -> LogScope:
        """ALB 로그엔 배포 구분이 없어 이 배포가 서비스한 구간(성공한 때~교체될 때)으로 나눈다."""
        detail = await self._deployment_history_service.get_deployment_request(
            owner_id, service_id, deployment_request_id
        )
        target_id = self._pick_target(detail, target_id)
        default_start = self._first_entered_at(detail, DeploymentStatus.SUCCEEDED)
        start, end = self._resolve_range(detail, default_start, start, end)
        if target_id is not None:
            await self._observability_service.get_target_kind(target_id)
        return LogScope(
            ObservabilityService.build_namespace(detail.service.id),
            target_id,
            [],
            # 서비스한 적 없는 배포(성공하지 못한 배포)는 범위가 없어 조회하지 않는다.
            start if default_start is not None else None,
            end if default_start is not None else None,
        )

    async def search_network_logs(
        self, scope: LogScope, limit: int, status_class: StatusClass | None
    ) -> list[NetworkLogEntry]:
        query = scope.query
        if query is None:
            return []
        target_id, start, end = query
        return await self._observability_service.search_network_logs(
            target_id, scope.namespace, _to_ns(start), _to_ns(end), limit, status_class
        )

    @staticmethod
    def _pick_target(detail: DeploymentDetail, target_id: int | None) -> int | None:
        allowed = [target.id for target in detail.targets]
        if target_id is None:
            return allowed[0] if allowed else None
        if target_id not in allowed:
            raise InvalidInputError("target is not part of this deployment", target_id=target_id)
        return target_id

    @staticmethod
    def _first_entered_at(detail: DeploymentDetail, status: DeploymentStatus) -> datetime | None:
        return next((h.created_at for h in detail.histories if h.to_status == status), None)

    @staticmethod
    def _resolve_range(
        detail: DeploymentDetail,
        default_start: datetime | None,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[datetime | None, datetime | None]:
        """요청한 구간을 검증하고, 없는 쪽은 배포 기간으로 채운다(최대 7일, 미래 제외)."""
        if start is not None or end is not None:
            resolved_end = end or _default_end(detail)
            resolved_start = start or max(
                default_start or resolved_end - MAX_RANGE, resolved_end - MAX_RANGE
            )
            ObservabilityService.validate_range(resolved_start, resolved_end)
            return resolved_start, resolved_end
        if default_start is None:
            return None, None
        resolved_end = _default_end(detail)
        resolved_start = max(default_start, resolved_end - MAX_RANGE)
        if resolved_start >= resolved_end:
            resolved_start = resolved_end - timedelta(minutes=1)
        return resolved_start, resolved_end


def _default_end(detail: DeploymentDetail) -> datetime:
    """교체된 배포는 교체된 때까지, 그 밖에는 지금까지다."""
    now = now_utc()
    if detail.replaced_by is not None:
        return min(detail.replaced_by.at, now)
    return now
