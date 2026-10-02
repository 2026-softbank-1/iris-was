from enum import StrEnum


class JobKind(StrEnum):
    BUILD = "BUILD"
    DEPLOY = "DEPLOY"
    RECONCILE = "RECONCILE"
    ROLLBACK = "ROLLBACK"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class AnalysisJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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


# 서비스·환경별로 동시에 하나만 허용하는 "진행 중" 상태.
ACTIVE_DEPLOYMENT_STATUSES = (
    DeploymentStatus.QUEUED,
    DeploymentStatus.BUILDING,
    DeploymentStatus.DEPLOYING,
)


class DeploymentTrigger(StrEnum):
    MANUAL = "MANUAL"
    PUSH = "PUSH"
    CLI = "CLI"
    REDEPLOY = "REDEPLOY"
    ROLLBACK = "ROLLBACK"


class ReleaseStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class FailureCode(StrEnum):
    BUILD_CONFIG_REQUIRED = "BUILD_CONFIG_REQUIRED"
    BUILD_FAILED = "BUILD_FAILED"
    DEPLOY_FAILED = "DEPLOY_FAILED"


class TargetKind(StrEnum):
    AWS = "AWS"
    LOCAL = "LOCAL"
