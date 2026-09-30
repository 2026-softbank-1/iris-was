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


class Builder(StrEnum):
    DOCKERFILE = "dockerfile"
    RAILPACK = "railpack"
