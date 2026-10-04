"""레포 구성 분석(Analysis Gate) 실행: 고정 커밋 소스 확보 → 분석기 gate CLI → 결과 기록.

Build Worker 프로세스 안에서 돈다. 빌드와 같은 GitHub App 설치 토큰·tarball 경로로 소스를 받는다.
분석은 소스를 읽기만 하고 외부 상태를 바꾸지 않으므로, 처리 중에 Worker 가 죽으면 lease 만료 뒤
처음부터 다시 실행한다.
"""

import asyncio
import gzip
import logging
import tarfile
import tempfile
import zlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.analysis_gate_client import (
    AnalysisGateClient,
    AnalysisGateResponse,
    AnalysisGateTimeoutError,
)
from app.clients.github_client import GitHubClient, SourceTooLargeError
from app.core.config import BuildWorkerSettings
from app.core.exceptions import (
    ArchiveInvalidError,
    ArchiveTooLargeError,
    ExternalError,
    ForbiddenError,
    NotConfiguredError,
    NotFoundError,
    RepositoryAnalysisFailedError,
)
from app.enums import AnalysisErrorCode, RepositoryAnalysisStatus
from app.models.repository_analysis import RepositoryAnalysis
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.repository_analysis_repository import RepositoryAnalysisRepository
from app.repositories.service_stack_repository import ServiceStackRepository
from app.schemas.analysis_gate import AnalysisGateRequest
from app.services.repository_url import parse_repository_url
from app.services.source_archive import ArchiveLimits
from app.services.stack_changes import compute_stack_changes

logger = logging.getLogger(__name__)

# 선점한 뒤 이 횟수를 넘겨 다시 선점되면(처리 중 Worker 가 계속 죽으면) 실패로 끝낸다.
MAX_ATTEMPTS = 3
# 소스 다운로드·압축 해제에 쓰는 여유. 분석기 제한 시간에 더해 lease 를 잡는다.
_LEASE_MARGIN = timedelta(minutes=5)
_MAX_ERROR_LENGTH = 1000
_CORRUPT_ARCHIVE_ERRORS = (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error)


class AnalysisGateService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        analyzer: AnalysisGateClient,
        settings: BuildWorkerSettings,
        worker_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._github = github
        self._analyzer = analyzer
        self._settings = settings
        self._worker_id = worker_id
        self._lease_duration = (
            timedelta(seconds=settings.analysis_gate_timeout_seconds) + _LEASE_MARGIN
        )

    async def claim_next_analysis(self) -> RepositoryAnalysis | None:
        async with self._session_factory.begin() as session:
            return await RepositoryAnalysisRepository(session).claim_next(
                self._worker_id, self._lease_duration
            )

    async def find_seconds_until_next_run(self) -> float | None:
        """분석은 예약 실행이 없다. 놓친 알림·만료된 lease 는 깨우기의 기본 주기로 회수한다."""
        return None

    async def run(self, analysis: RepositoryAnalysis) -> None:
        """분석 1건을 끝까지 처리하고 결과(성공·실패)를 기록한다."""
        if analysis.attempts > MAX_ATTEMPTS:
            await self._record_failure(
                analysis.id,
                RepositoryAnalysisFailedError(
                    AnalysisErrorCode.ANALYSIS_INTERRUPTED, "analysis attempts exhausted"
                ),
            )
            return
        try:
            response = await self._analyze(analysis)
        except RepositoryAnalysisFailedError as exc:
            logger.info(
                "repository analysis failed",
                extra={"action": "run_analysis", "error_code": exc.error_code, **exc.fields},
            )
            await self._record_failure(analysis.id, exc)
            return
        await self._record_success(analysis.id, response)

    async def release(self, analysis_id: int) -> None:
        """종료 신호로 멈춘 분석을 대기열로 돌려 다른 Worker 가 바로 이어 가게 한다."""
        async with self._session_factory.begin() as session:
            await RepositoryAnalysisRepository(session).release(analysis_id, self._worker_id)

    async def _analyze(self, analysis: RepositoryAnalysis) -> AnalysisGateResponse:
        owner, repository_name = parse_repository_url(analysis.source_repository_url)
        full_name = f"{owner}/{repository_name}"
        token = await self._create_token(analysis, repository_name)
        source_sha = analysis.source_sha
        with tempfile.TemporaryDirectory(prefix="iris-analysis-") as temp_dir:
            snapshot_path = Path(temp_dir) / "source.tar.gz"
            source_root = Path(temp_dir) / "source"
            try:
                if source_sha is None:
                    source_sha = await self._github.get_branch_sha(
                        token, full_name, analysis.source_branch
                    )
                    await self._record_source_sha(analysis.id, source_sha)
                await self._github.download_tarball(
                    token, full_name, source_sha, snapshot_path, self._settings.snapshot_max_bytes
                )
            except NotFoundError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_REF_NOT_FOUND, "source commit not found"
                ) from exc
            except ForbiddenError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_NOT_ACCESSIBLE, "source is not accessible"
                ) from exc
            except SourceTooLargeError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_TOO_LARGE, "source exceeds the size limit"
                ) from exc
            except ExternalError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_NOT_ACCESSIBLE, "source download failed"
                ) from exc
            limits = ArchiveLimits(
                max_uncompressed_bytes=self._settings.upload_max_uncompressed_bytes,
                max_entries=self._settings.upload_max_entries,
            )
            try:
                await asyncio.to_thread(extract_source_snapshot, snapshot_path, source_root, limits)
            except ArchiveTooLargeError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_TOO_LARGE, exc.message
                ) from exc
            except ArchiveInvalidError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.SOURCE_INVALID, exc.message
                ) from exc
            snapshot_path.unlink()
            request = AnalysisGateRequest(
                source_root=source_root,
                root_directory=analysis.root_directory or ".",
                source_sha=source_sha,
                mode=analysis.mode,
            )
            try:
                return await self._analyzer.analyze(request)
            except NotConfiguredError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.ANALYZER_UNAVAILABLE, "analyzer is unavailable"
                ) from exc
            except AnalysisGateTimeoutError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.ANALYZER_TIMED_OUT, "analyzer timed out"
                ) from exc
            except ExternalError as exc:
                raise RepositoryAnalysisFailedError(
                    AnalysisErrorCode.ANALYZER_FAILED, exc.message, **exc.fields
                ) from exc

    async def _create_token(self, analysis: RepositoryAnalysis, repository_name: str) -> str:
        if analysis.github_installation_id is None:
            raise RepositoryAnalysisFailedError(
                AnalysisErrorCode.SOURCE_NOT_ACCESSIBLE, "github installation is missing"
            )
        async with self._session_factory() as session:
            installation = await GithubInstallationRepository(session).get_by_id(
                analysis.github_installation_id
            )
        try:
            return await self._github.create_installation_token(
                installation.installation_id, repository_name
            )
        except (NotFoundError, ForbiddenError, ExternalError) as exc:
            raise RepositoryAnalysisFailedError(
                AnalysisErrorCode.SOURCE_NOT_ACCESSIBLE, "github installation token unavailable"
            ) from exc

    async def _record_source_sha(self, analysis_id: int, source_sha: str) -> None:
        async with self._session_factory.begin() as session:
            analysis = await RepositoryAnalysisRepository(session).get_by_id(
                analysis_id, for_update=True
            )
            analysis.source_sha = source_sha

    async def _record_success(self, analysis_id: int, response: AnalysisGateResponse) -> None:
        async with self._session_factory.begin() as session:
            analysis = await RepositoryAnalysisRepository(session).get_by_id(
                analysis_id, for_update=True
            )
            if not self._is_still_owned(analysis):
                return
            analysis.succeed(response.result.decision, response.result.complexity, response.raw)
            if analysis.stack_id is not None:
                await self._record_stack_changes(session, analysis)
        logger.info(
            "repository analysis succeeded",
            extra={
                "action": "run_analysis",
                "decision": response.result.decision,
                "complexity": response.result.complexity,
                "unit_count": len(response.result.units),
            },
        )

    async def _record_stack_changes(
        self, session: AsyncSession, analysis: RepositoryAnalysis
    ) -> None:
        """스택 재분석이면 기준 분석과 비교해 스택에 pendingChanges 를 남긴다(같은 트랜잭션)."""
        assert analysis.stack_id is not None and analysis.result is not None
        stack = await ServiceStackRepository(session).get_by_id(analysis.stack_id, for_update=True)
        if analysis.id <= stack.analysis_id:
            return
        baseline = await RepositoryAnalysisRepository(session).get_by_id(stack.analysis_id)
        changes = compute_stack_changes(baseline.result, analysis.result)
        stack.record_pending_changes(analysis.id, analysis.source_sha, changes)
        logger.info(
            "stack changes recorded",
            extra={
                "action": "run_analysis",
                "stack_id": stack.id,
                "change_count": len(changes),
            },
        )

    async def _record_failure(self, analysis_id: int, error: RepositoryAnalysisFailedError) -> None:
        async with self._session_factory.begin() as session:
            analysis = await RepositoryAnalysisRepository(session).get_by_id(
                analysis_id, for_update=True
            )
            if not self._is_still_owned(analysis):
                return
            analysis.fail(error.error_code, error.message[:_MAX_ERROR_LENGTH])

    def _is_still_owned(self, analysis: RepositoryAnalysis) -> bool:
        # lease 가 만료돼 다른 Worker 가 가져갔으면 그쪽 결과를 남긴다.
        is_owned = (
            analysis.status == RepositoryAnalysisStatus.RUNNING
            and analysis.locked_by == self._worker_id
        )
        if not is_owned:
            logger.warning("repository analysis lease lost", extra={"action": "run_analysis"})
        return is_owned


def extract_source_snapshot(archive_path: Path, target: Path, limits: ArchiveLimits) -> None:
    """GitHub tarball 을 최상위 디렉터리를 벗겨 target 에 푼다. 분석에 필요한 것만 남긴다.

    일반 파일과 디렉터리만 푼다. 심볼릭·하드 링크와 장치 파일은 분석기가 따라가지 않으므로 버린다.
    경로 이탈은 tarfile 의 data 필터가 거른다. 풀린 크기·항목 수는 업로드와 같은 한도를 쓴다.
    """
    target.mkdir(parents=True, exist_ok=True)
    entries = 0
    uncompressed_bytes = 0
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                entries += 1
                if entries > limits.max_entries:
                    raise ArchiveTooLargeError("source has too many entries", entries=entries)
                if not (member.isfile() or member.isdir()):
                    continue
                relative = member.name.partition("/")[2].strip("/")
                if not relative:
                    continue
                if member.isfile():
                    uncompressed_bytes += member.size
                    if uncompressed_bytes > limits.max_uncompressed_bytes:
                        raise ArchiveTooLargeError("source exceeds the uncompressed size limit")
                member.name = relative
                try:
                    archive.extract(member, target, filter="data")
                except tarfile.FilterError as exc:
                    raise ArchiveInvalidError("source archive has an unsafe entry") from exc
    except _CORRUPT_ARCHIVE_ERRORS as exc:
        raise ArchiveInvalidError("source archive is corrupt") from exc
