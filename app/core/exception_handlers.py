import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    AppError,
    InvalidInputError,
    TooManyRequestsError,
    VariablesInvalidError,
)
from app.schemas.response import ApiResponse, ErrorDetail

logger = logging.getLogger(__name__)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: Mapping[str, str] | None = None,
    data: Any = None,
) -> JSONResponse:
    body = ApiResponse[Any](success=False, code=code, message=message, details=details, data=data)
    return JSONResponse(
        body.model_dump(mode="json", by_alias=True, exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    extra = {"action": "handle_app_error", "error_code": exc.code, **exc.fields}
    if exc.status_code >= 500:
        logger.error("request failed", extra=extra, exc_info=exc)
    else:
        logger.info("request rejected", extra=extra)
    headers = (
        {"Retry-After": str(exc.retry_after_seconds)}
        if isinstance(exc, TooManyRequestsError)
        else None
    )
    details = (
        [ErrorDetail(field=issue.field, reason=issue.reason) for issue in exc.issues]
        if isinstance(exc, InvalidInputError) and exc.issues
        else None
    )
    # 환경변수 검증 실패는 웹이 이슈(키·코드·제안)를 그대로 그리도록 data 에 검증 결과를 싣는다.
    data = exc.data if isinstance(exc, VariablesInvalidError) else None
    return error_response(exc.status_code, exc.code, exc.message, details, headers, data)


async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # loc 첫 요소는 위치(body·query·path)라 뺀다. 필드명은 요청 스키마의 alias(camelCase) 그대로다.
    details = [
        ErrorDetail(
            field=".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
            reason=error["msg"],
        )
        for error in exc.errors()
    ]
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", "request validation failed", details
    )


async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 없는 경로(404)·허용되지 않은 메서드(405) 등 프레임워크 오류도 같은 봉투로 바꾼다.
    return error_response(
        exc.status_code, HTTPStatus(exc.status_code).name, str(exc.detail), headers=exc.headers
    )


def register_exception_handlers(app: FastAPI) -> None:
    # Starlette 타입 힌트가 핸들러 인자를 Exception 으로만 받아 하위 타입에 ignore 를 단다.
    app.add_exception_handler(AppError, handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)  # type: ignore[arg-type]
