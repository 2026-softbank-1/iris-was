"""배포 방식(롤링·카나리·블루그린) 규칙. 서비스 설정 저장·배포 요청 생성·Deploy Worker 가 함께 쓴다.

단계와 대기 시간은 iris-service chart 가 정한다. 여기서는 어떤 방식을 적용할지와 release 기한에
더할 대기 시간만 정한다.
"""

from datetime import timedelta

from app.enums import DeploymentStrategy

# 카나리·블루그린은 새 Pod 와 이전 Pod 를 함께 띄우는 방식이라 Pod 가 2개 이상이어야 한다.
MIN_PROGRESSIVE_REPLICAS = 2
PROGRESSIVE_STRATEGIES = frozenset({DeploymentStrategy.CANARY, DeploymentStrategy.BLUE_GREEN})
# chart 의 고정 대기: 카나리는 새 Pod 1개를 60초 관찰, 블루그린은 전환 전 30초·이전 묶음 정리 30초.
_EXTRA_WAIT = {
    DeploymentStrategy.CANARY: timedelta(seconds=60),
    DeploymentStrategy.BLUE_GREEN: timedelta(seconds=60),
}


def resolve_deployment_strategy(
    requested: DeploymentStrategy, replicas: int, *, is_enabled: bool
) -> DeploymentStrategy:
    """배포 요청에 실제로 적용할 방식. 기능이 꺼져 있거나 Pod 가 2개 미만이면 ROLLING 이다."""
    if requested in PROGRESSIVE_STRATEGIES and (
        not is_enabled or replicas < MIN_PROGRESSIVE_REPLICAS
    ):
        return DeploymentStrategy.ROLLING
    return requested


def strategy_extra_wait(strategy: DeploymentStrategy | None) -> timedelta:
    """release 기한에 더할 방식별 고정 대기 시간. 기능 도입 전 요청(None)은 롤링과 같다."""
    if strategy is None:
        return timedelta()
    return _EXTRA_WAIT.get(strategy, timedelta())
