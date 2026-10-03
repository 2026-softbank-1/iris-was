"""실패가 확정된 배포를 사용자 없이 자동으로 진단한다.

배포 요청의 상태는 Worker 프로세스가 바꾸는데, 에이전트·Loki 설정은 Control API 에만 있다.
그래서 Worker 가 부르는 대신 Control API 안에서 주기적으로 진단할 배포를 찾는다. 이 방식은
서버가 재시작·교체되는 동안 놓친 실패도 다음 주기에 이어 받는다. 진단 행을 하나씩 먼저 커밋해
여러 Pod 가 같은 배포를 중복 진단하지 않는다(배포 요청마다 진행 중 행은 하나뿐이다).
"""

import asyncio
import contextlib
import logging

from app.services.diagnosis_service import DiagnosisServiceOpener, run_diagnosis_in_background

logger = logging.getLogger(__name__)

# 종료 신호를 받은 뒤 진행 중인 진단을 기다려 주는 시간. 넘기면 취소하고, 남은 진행 중 행은
# 다른 Pod 가 낡은 행으로 보고 4분 뒤 다시 시작한다. 진단은 보통 30~65초 걸려서, 롤아웃과 겹친
# 진단을 대부분 끝낼 수 있게 60초로 둔다. Pod 의 종료 유예(iris-infra 의
# `api.terminationGracePeriodSeconds`, 90초)보다 짧아야 취소·정리할 시간이 남는다.
SHUTDOWN_GRACE_SECONDS = 60.0


class AutoDiagnosisRunner:
    def __init__(self, open_service: DiagnosisServiceOpener, interval_seconds: float) -> None:
        self._open_service = open_service
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(self._stop), name="auto-diagnosis")

    async def stop(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """멈추라고 알리고 진행 중인 진단이 끝나길 `grace_seconds` 까지 기다린다."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=grace_seconds)
        except TimeoutError:
            logger.warning(
                "auto diagnosis cancelled while a diagnosis was running",
                extra={"action": "stop_auto_diagnosis"},
            )
        self._task = None

    async def run(self, stop: asyncio.Event) -> None:
        """`stop` 이 켜질 때까지 진단할 배포를 찾아 하나씩 끝까지 진단한다."""
        logger.info(
            "auto diagnosis started",
            extra={"action": "run_auto_diagnosis", "interval_seconds": self._interval_seconds},
        )
        while not stop.is_set():
            try:
                has_worked = await self.run_once()
            except Exception:
                # 반복 작업의 경계다. DB 장애 같은 오류에도 루프가 죽지 않게 기록하고 다시 한다.
                logger.exception(
                    "auto diagnosis iteration failed", extra={"action": "run_auto_diagnosis"}
                )
                has_worked = False
            if has_worked:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_seconds)

    async def run_once(self) -> bool:
        """진단할 배포가 있으면 시작해 끝까지 진단하고 True, 없으면 False 다."""
        async with self._open_service() as service:
            started = await service.start_next_automatic_diagnosis()
        if started is None:
            return False
        await run_diagnosis_in_background(
            self._open_service,
            started.owner_id,
            started.service_id,
            started.deployment_request_id,
            started.diagnosis_id,
        )
        return True
