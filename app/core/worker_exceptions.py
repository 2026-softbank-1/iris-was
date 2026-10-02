from app.core.exceptions import AppError, ConflictError
from app.enums import FailureCode


class JobLeaseLostError(ConflictError):
    code = "JOB_LEASE_LOST"


class BuildExecutionError(AppError):
    code = "BUILD_EXECUTION_FAILED"

    def __init__(self, failure_code: FailureCode, message: str | None = None) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class GitOpsConflictError(ConflictError):
    code = "GITOPS_CONFLICT"
    retryable = True
