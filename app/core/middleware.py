import logging
from http import HTTPStatus
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exception_handlers import error_response
from app.core.logging import log_context

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# K8s probe 가 수 초마다 호출하므로 접근 로그에서 뺀다.
_UNLOGGED_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware:
    """요청마다 request_id 를 로그 컨텍스트와 응답 헤더에 싣고, 접근 로그 1줄을 남긴다.

    처리되지 않은 예외는 여기서 한 번만 기록하고 500 공통 봉투로 응답한다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        started_at = perf_counter()
        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                logger.exception("unhandled exception", extra={"action": "handle_request"})
                if response_started:
                    raise
                response = error_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "internal server error"
                )
                await response(scope, receive, send_with_request_id)
            finally:
                if scope["path"] not in _UNLOGGED_PATHS:
                    route = scope.get("route")
                    logger.info(
                        "request completed",
                        extra={
                            "action": "handle_request",
                            "method": scope["method"],
                            # 경로 템플릿(/deployments/{id})으로 남겨야 경로별 집계가 된다.
                            "route": getattr(route, "path", scope["path"]),
                            "status_code": status_code,
                            "duration_ms": round((perf_counter() - started_at) * 1000, 1),
                        },
                    )
