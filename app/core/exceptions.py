"""도메인 예외 계층: AppError → 카테고리 → 도메인 예외.

- code: 클라이언트가 분기하는 고정 코드(계약). 한 번 정하면 바꾸지 않는다.
- status_code: API 가 응답할 HTTP 상태.
- retryable: Worker 가 RETRY_WAIT(True) / FAILED(False) 를 가르는 기준.
- fields: 로그에 남길 식별자. 메시지에 넣지 않고 여기에 담는다.

raise 는 Service·Repository·Client 에서 하고, HTTP 변환은 exception_handlers 한곳에서 한다.
도메인 예외는 클라이언트 분기나 재시도 정책이 다를 때만 카테고리를 상속해 만든다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from app.enums import FailureCode


class AppError(Exception):
    code: ClassVar[str] = "INTERNAL_ERROR"
    status_code: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(self, message: str | None = None, **fields: object) -> None:
        self.message = message or self.code
        self.fields = fields
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


@dataclass(frozen=True)
class FieldIssue:
    """입력의 어느 부분이 왜 틀렸는지. 응답의 `details` 가 된다. 값은 담지 않는다."""

    field: str
    reason: str


class InvalidInputError(AppError):
    """도메인 규칙상 받을 수 없는 입력. 스키마 검증 실패(VALIDATION_ERROR)와 구분한다.

    틀린 위치가 여러 곳이면 issues 에 담아 응답 `details` 로 알린다. message 는 계약이다.
    """

    code = "INVALID_INPUT"
    status_code = 422

    def __init__(
        self, message: str | None = None, *, issues: Sequence[FieldIssue] = (), **fields: object
    ) -> None:
        super().__init__(message, **fields)
        self.issues = tuple(issues)


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


class TooManyRequestsError(AppError):
    """허용된 빈도보다 빠른 요청. 몇 초 뒤에 다시 보내면 되는지 retry_after_seconds 에 담는다."""

    code = "TOO_MANY_REQUESTS"
    status_code = 429

    def __init__(
        self, message: str | None = None, *, retry_after_seconds: int, **fields: object
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds, **fields)
        self.retry_after_seconds = retry_after_seconds


class ExternalError(AppError):
    """외부 시스템(CodeBuild·Git·Argo CD) 호출 실패. Client 가 SDK 예외를 이것으로 바꾼다."""

    code = "EXTERNAL_ERROR"
    status_code = 502
    retryable = True


class NotConfiguredError(AppError):
    """필요한 설정(시크릿 등)이 없어 기능을 쓸 수 없다. 어떤 설정인지는 fields 에 담는다."""

    code = "NOT_CONFIGURED"
    status_code = 503


class RepositoryNotAccessibleError(ForbiddenError):
    """GitHub App 이 설치되지 않았거나 권한을 주지 않은 저장소. 설치·권한 확인을 안내한다."""

    code = "REPOSITORY_NOT_ACCESSIBLE"


class ProjectNotFoundError(NotFoundError):
    code = "PROJECT_NOT_FOUND"


class ServiceNotFoundError(NotFoundError):
    code = "SERVICE_NOT_FOUND"


class DeploymentRequestNotFoundError(NotFoundError):
    code = "DEPLOYMENT_REQUEST_NOT_FOUND"


class VariableNotFoundError(NotFoundError):
    code = "VARIABLE_NOT_FOUND"


class UploadNotFoundError(NotFoundError):
    """모르는 업로드이거나 다른 서비스의 업로드다. 둘을 구분해 알리지 않는다."""

    code = "UPLOAD_NOT_FOUND"


class UploadUnavailableError(ConflictError):
    """업로드가 이미 배포 요청에 쓰였거나 만료됐다. 다시 올려야 한다."""

    code = "UPLOAD_UNAVAILABLE"


class UploadTooLargeError(AppError):
    """업로드가 크기 한도(압축한 바이트)를 넘었다. 본문을 다 읽기 전에 거절한다."""

    code = "UPLOAD_TOO_LARGE"
    status_code = 413


class UploadNotGzipError(AppError):
    """본문이 gzip 이 아니다(매직 바이트 불일치)."""

    code = "UPLOAD_NOT_GZIP"
    status_code = 415


class ArchiveInvalidError(AppError):
    """소스 아카이브가 손상됐거나 허용하지 않는 항목을 담고 있다. 재시도해도 같다."""

    code = "ARCHIVE_INVALID"
    status_code = 422


class ArchiveTooLargeError(ArchiveInvalidError):
    """아카이브를 풀었을 때의 크기나 항목 수가 한도를 넘는다(압축 폭탄 방어)."""

    code = "ARCHIVE_TOO_LARGE"


class DiagnosisNotFoundError(NotFoundError):
    code = "DIAGNOSIS_NOT_FOUND"


class DeploymentNotFailedError(ConflictError):
    """실패하지 않은 배포는 진단하지 않는다. 현재 상태를 fields 에 담는다."""

    code = "DEPLOYMENT_NOT_FAILED"


class DiagnosisInProgressError(ConflictError):
    """같은 배포 요청을 진단하는 중이다. 끝난 뒤 결과를 조회한다."""

    code = "DIAGNOSIS_IN_PROGRESS"


class DiagnosisLogsUnavailableError(ConflictError):
    """진단할 로그가 없다. 로그 없이 원인을 추측하지 않는다."""

    code = "DIAGNOSIS_LOGS_UNAVAILABLE"


class DiagnosisAgentError(ExternalError):
    """에러 진단 에이전트 호출 실패. 에이전트가 준 오류 코드를 agent_code 에 담는다.

    에이전트의 메시지는 입력 일부를 담을 수 있어 옮기지 않는다.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        agent_code: str | None = None,
        agent_status: int | None = None,
        **fields: object,
    ) -> None:
        super().__init__(message, agent_code=agent_code, agent_status=agent_status, **fields)
        self.agent_code = agent_code
        self.agent_status = agent_status


class ProjectNameConflictError(ConflictError):
    code = "PROJECT_NAME_CONFLICT"


class ServiceNameConflictError(ConflictError):
    code = "SERVICE_NAME_CONFLICT"


class VariableConflictError(ConflictError):
    code = "VARIABLE_CONFLICT"


class VariableDecryptionError(AppError):
    """저장된 변수 값을 복호화하지 못했다. 암호화 키가 바뀌었거나 값이 손상됐다."""

    code = "VARIABLE_DECRYPTION_FAILED"


class DeploymentInProgressError(ConflictError):
    """같은 서비스·환경에 진행 중인 배포가 있어 새 배포 요청을 만들 수 없다."""

    code = "DEPLOYMENT_IN_PROGRESS"


class NoSucceededDeploymentError(ConflictError):
    """재시작·삭제할 배포가 없다. 성공한 배포가 없거나 이미 서비스를 내렸다."""

    code = "NO_SUCCEEDED_DEPLOYMENT"


class InvalidStatusTransitionError(ConflictError):
    """배포 요청 상태 전이 표에 없는 이동. 현재 상태와 요청한 상태를 fields 에 담는다."""

    code = "INVALID_STATUS_TRANSITION"


class OnpremServerNotFoundError(NotFoundError):
    """모르는 서버이거나 다른 사용자의 서버다. 둘을 구분해 알리지 않는다."""

    code = "ONPREM_SERVER_NOT_FOUND"


class OnpremServerNameConflictError(ConflictError):
    code = "ONPREM_SERVER_NAME_CONFLICT"


class OnpremServerLimitExceededError(ConflictError):
    """사용자마다 등록할 수 있는 서버 수를 넘었다. 한도는 fields 의 limit 이다."""

    code = "ONPREM_SERVER_LIMIT_EXCEEDED"


class OnpremServerInUseError(ConflictError):
    """서버 타깃에 서비스가 붙어 있거나 그 서비스의 배포가 진행 중이라 지울 수 없다."""

    code = "ONPREM_SERVER_IN_USE"


class OnpremServerNotConnectedError(ConflictError):
    """서버 비밀은 맞지만 서버가 아직 CONNECTED 가 아니다. 서버는 다음 회차에 다시 부른다."""

    code = "ONPREM_SERVER_NOT_CONNECTED"


class InvalidRegistrationTokenError(UnauthorizedError):
    """등록 토큰이 없거나 만료됐거나 이미 연결된 서버의 토큰이다. 셋을 구분해 알리지 않는다."""

    code = "INVALID_REGISTRATION_TOKEN"


class TargetNotConnectedError(ConflictError):
    """배포 타깃이 등록한 서버인데 아직 연결되지 않았다(CONNECTED 가 아니다)."""

    code = "TARGET_NOT_CONNECTED"


class GitOpsConflictError(ConflictError):
    """GitOps 브랜치가 그새 움직여 fast-forward 할 수 없다. HEAD 위에 커밋을 다시 만든다."""

    code = "GITOPS_CONFLICT"


class BuildFailedError(AppError):
    """빌드를 더 진행할 수 없는 실패. 재시도하지 않고 failure_code 로 배포 요청을 끝낸다."""

    code = "BUILD_FAILED"
    status_code = 422

    def __init__(
        self, failure_code: FailureCode, message: str | None = None, **fields: object
    ) -> None:
        super().__init__(message or failure_code, **fields)
        self.failure_code = failure_code
