"""도메인 예외 계층: AppError → 카테고리 → 도메인 예외.

- code: 클라이언트가 분기하는 고정 코드(계약). 한 번 정하면 바꾸지 않는다.
- status_code: API 가 응답할 HTTP 상태.
- retryable: Worker 가 RETRY_WAIT(True) / FAILED(False) 를 가르는 기준.
- fields: 로그에 남길 식별자. 메시지에 넣지 않고 여기에 담는다.

raise 는 Service·Repository·Client 에서 하고, HTTP 변환은 exception_handlers 한곳에서 한다.
도메인 예외는 클라이언트 분기나 재시도 정책이 다를 때만 카테고리를 상속해 만든다.
"""

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


class InvalidInputError(AppError):
    """도메인 규칙상 받을 수 없는 입력. 스키마 검증 실패(VALIDATION_ERROR)와 구분한다."""

    code = "INVALID_INPUT"
    status_code = 422


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


class ExternalError(AppError):
    """외부 시스템(CodeBuild·Git·Argo CD) 호출 실패. Client 가 SDK 예외를 이것으로 바꾼다."""

    code = "EXTERNAL_ERROR"
    status_code = 502
    retryable = True


class BuildFailedError(AppError):
    """빌드를 더 진행할 수 없는 실패. 재시도하지 않고 failure_code 로 배포 요청을 끝낸다."""

    code = "BUILD_FAILED"
    status_code = 422

    def __init__(
        self, failure_code: FailureCode, message: str | None = None, **fields: object
    ) -> None:
        super().__init__(message or failure_code, **fields)
        self.failure_code = failure_code
