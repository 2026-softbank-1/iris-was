import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import get_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.routers import (
    auth_router,
    deployment_router,
    github_router,
    observability_router,
    project_router,
    service_router,
    target_router,
    user_router,
    webhook_router,
)

configure_logging("control-api", get_settings().log_level)
logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0

HTTP_CLIENT_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 외부 API(GitHub) 호출용 클라이언트는 앱 수명 동안 하나를 공유한다.
    async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT_SECONDS) as http_client:
        app.state.http_client = http_client
        yield
    await get_engine().dispose()


OPENAPI_TAGS = [
    {"name": "observability", "description": "서비스 런타임 로그·메트릭·SSE"},
    {"name": "auth", "description": "GitHub 로그인·로그아웃"},
    {"name": "user", "description": "현재 사용자"},
    {"name": "github", "description": "GitHub App 설치와 저장소·브랜치 조회"},
    {"name": "projects", "description": "서비스를 묶는 프로젝트"},
    {"name": "services", "description": "저장소와 연결된 서비스(사용자 앱)"},
    {"name": "deployments", "description": "서비스의 배포 요청 생성·목록·상세(상태 이력)"},
    {"name": "targets", "description": "배포 타깃(aws · local)"},
    {"name": "webhooks", "description": "외부 서비스(GitHub)가 호출하는 웹훅. 서명으로 인증한다"},
]

app = FastAPI(
    title="AnyDeploy Control API",
    description=(
        "모든 JSON 응답은 `ApiResponse` 봉투(`success`·`code`·`message`·`data`)로 감싼다. "
        "필드는 camelCase 다. 인증은 쿠키(웹) 또는 `Authorization: Bearer`(CLI)."
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)
app.include_router(auth_router.router)
app.include_router(observability_router.router)
app.include_router(user_router.router)
app.include_router(github_router.router)
app.include_router(project_router.router)
app.include_router(service_router.router)
app.include_router(deployment_router.router)
app.include_router(target_router.router)
app.include_router(webhook_router.router)


@app.get("/healthz", status_code=status.HTTP_204_NO_CONTENT)
async def check_health() -> Response:
    # 프로세스 생존만 본다. 의존성 장애로 모든 Pod 가 재시작되지 않도록 DB 는 보지 않는다.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/readyz", status_code=status.HTTP_204_NO_CONTENT)
async def check_readiness(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> Response:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        logger.warning(
            "database not ready",
            extra={"action": "check_readiness", "exc_type": type(exc).__name__},
        )
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
