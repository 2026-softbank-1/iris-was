"""Console Gateway 진입점(ADR 0033). 서비스 콘솔(Pod 셸) 연결을 중계한다.

    uvicorn app.console_gateway.main:app --host 0.0.0.0 --port 8080

Control API 와 같은 이미지에서 실행 명령만 다르다. DB 접속 정보를 갖지 않는다.
"""

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status

from app.clients.kubernetes_client import build_cluster_ssl_context
from app.console_gateway.cors import OriginCorsMiddleware
from app.console_gateway.dependencies import build_console_gateway_service
from app.console_gateway.router import router
from app.core.config import get_console_gateway_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.services.console_gateway_service import ConsoleGatewayService

logger = logging.getLogger(__name__)

# 클러스터 REST 호출 제한 시간(초). exec WebSocket 의 연결 제한은 Client 가 따로 둔다.
CLUSTER_HTTP_TIMEOUT_SECONDS = 10.0


def create_app(
    service: ConsoleGatewayService | None = None, allowed_origins: Iterable[str] = ()
) -> FastAPI:
    """service 를 주면 설정을 읽지 않는다(테스트). 안 주면 시작할 때 설정에서 만든다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            yield
            return
        settings = get_console_gateway_settings()
        configure_logging("console-gateway", settings.log_level)
        ssl_context = build_cluster_ssl_context(settings.console_aws_cluster_ca)
        async with httpx.AsyncClient(
            base_url=str(settings.console_aws_cluster_endpoint),
            verify=ssl_context,
            timeout=CLUSTER_HTTP_TIMEOUT_SECONDS,
        ) as http_client:
            app.state.console_allowed_origins = frozenset(settings.allowed_origins)
            app.state.console_gateway_service = build_console_gateway_service(
                settings, http_client, ssl_context
            )
            logger.info("console gateway started", extra={"action": "start_console_gateway"})
            yield

    # 문서 화면·스키마는 열지 않는다. 계약은 docs/console-api.md 다.
    app = FastAPI(
        title="AnyDeploy Console Gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.console_allowed_origins = frozenset(allowed_origins)
    if service is not None:
        app.state.console_gateway_service = service
    app.add_middleware(RequestContextMiddleware)
    # 마지막에 추가한 미들웨어가 가장 바깥이다. 에러 응답에도 CORS 헤더가 붙어야 한다.
    app.add_middleware(OriginCorsMiddleware)
    register_exception_handlers(app)
    app.include_router(router)

    @app.get("/healthz", status_code=status.HTTP_204_NO_CONTENT)
    async def check_health() -> Response:
        # 프로세스 생존만 본다. 클러스터 장애로 Pod 가 빠지지 않게 클러스터는 부르지 않는다.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/readyz", status_code=status.HTTP_204_NO_CONTENT)
    async def check_readiness(request: Request) -> Response:
        # 설정을 읽고 서비스를 만들었는지만 본다.
        if getattr(request.app.state, "console_gateway_service", None) is None:
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_app()
