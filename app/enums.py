from enum import StrEnum


class JobKind(StrEnum):
    BUILD = "BUILD"
    DEPLOY = "DEPLOY"
    RECONCILE = "RECONCILE"
    ROLLBACK = "ROLLBACK"
    REMOVE = "REMOVE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class Builder(StrEnum):
    DOCKERFILE = "dockerfile"
    RAILPACK = "railpack"


class Environment(StrEnum):
    PROD = "prod"


class DeploymentStatus(StrEnum):
    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    # 진행 중에 더 새로운 요청이 대신해 중단됐다. Worker 가 cancel_requested_at 을 보고 끝낸다.
    SUPERSEDED = "SUPERSEDED"


# 서비스·환경별로 동시에 하나만 허용하는 "진행 중" 상태.
ACTIVE_DEPLOYMENT_STATUSES = (
    DeploymentStatus.QUEUED,
    DeploymentStatus.BUILDING,
    DeploymentStatus.DEPLOYING,
)

# 앱 컨테이너가 listen 하는 포트. Deploy Worker 가 chart 에 넘기고 `PORT` 환경변수로 주입된다.
APP_PORT = 8080


class DeploymentTrigger(StrEnum):
    MANUAL = "MANUAL"
    PUSH = "PUSH"
    CLI = "CLI"
    REDEPLOY = "REDEPLOY"
    ROLLBACK = "ROLLBACK"
    RESTART = "RESTART"
    REMOVE = "REMOVE"


class DeploymentStrategy(StrEnum):
    """새 Pod 로 바꾸는 방식. 단계·대기 시간은 iris-service chart 가 정한다."""

    ROLLING = "ROLLING"
    CANARY = "CANARY"
    BLUE_GREEN = "BLUE_GREEN"


class BuildStatus(StrEnum):
    PENDING = "PENDING"
    SNAPSHOTTING = "SNAPSHOTTING"
    BUILDING = "BUILDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReleaseStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class FailureCode(StrEnum):
    SOURCE_NOT_ACCESSIBLE = "SOURCE_NOT_ACCESSIBLE"
    SOURCE_REF_NOT_FOUND = "SOURCE_REF_NOT_FOUND"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    # 올린 소스 아카이브가 손상됐거나 허용되지 않는 항목(경로 이탈·장치 파일 등)을 담고 있다.
    SOURCE_INVALID = "SOURCE_INVALID"
    BUILD_CONFIG_REQUIRED = "BUILD_CONFIG_REQUIRED"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_TIMED_OUT = "BUILD_TIMED_OUT"
    BUILD_INFRA_ERROR = "BUILD_INFRA_ERROR"
    DEPLOY_FAILED = "DEPLOY_FAILED"
    DEPLOY_TIMED_OUT = "DEPLOY_TIMED_OUT"
    DEPLOY_INFRA_ERROR = "DEPLOY_INFRA_ERROR"


class DiagnosisStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TargetKind(StrEnum):
    AWS = "AWS"
    ONPREM = "ONPREM"


class CliLoginSessionStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    # 만료됐거나 토큰을 이미 내줬다. 토큰을 내준 세션은 consumed_at 이 채워진다.
    EXPIRED = "EXPIRED"
