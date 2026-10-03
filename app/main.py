import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.core.database import get_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.dependencies import build_automatic_repair_opener, build_diagnosis_service_opener
from app.routers import (
    auth_router,
    cli_login_router,
    deployment_router,
    diagnosis_router,
    domain_router,
    github_router,
    observability_router,
    project_router,
    repair_router,
    scaling_router,
    service_router,
    target_router,
    upload_router,
    user_router,
    variable_router,
    webhook_router,
)
from app.services.auto_diagnosis import AutoDiagnosisRunner
from app.services.automatic_repair_service import AutomaticRepairRunner

configure_logging("control-api", get_settings().log_level)
logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0

HTTP_CLIENT_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # 외부 API(GitHub) 호출용 클라이언트는 앱 수명 동안 하나를 공유한다.
    async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT_SECONDS) as http_client:
        app.state.http_client = http_client
        auto_diagnosis = _start_auto_diagnosis(settings, http_client)
        automatic_repair = None
        if all(
            (
                settings.repair_agent_url,
                settings.repair_agent_api_key,
                settings.github_app_id,
                settings.github_app_private_key,
            )
        ):
            automatic_repair = AutomaticRepairRunner(
                build_automatic_repair_opener(settings, http_client)
            )
            automatic_repair.start()
        try:
            yield
        finally:
            if automatic_repair is not None:
                await automatic_repair.stop()
            if auto_diagnosis is not None:
                await auto_diagnosis.stop()
    await get_engine().dispose()


def _start_auto_diagnosis(
    settings: Settings, http_client: httpx.AsyncClient
) -> AutoDiagnosisRunner | None:
    """실패가 확정된 배포를 자동으로 진단하는 반복 작업을 켠다. 에이전트가 없으면 켜지 않는다."""
    is_agent_configured = (
        settings.diagnosis_agent_url is not None and settings.diagnosis_agent_api_key is not None
    )
    if not settings.diagnosis_auto_start_enabled or not is_agent_configured:
        logger.info(
            "auto diagnosis is off",
            extra={
                "action": "start_auto_diagnosis",
                "is_enabled": settings.diagnosis_auto_start_enabled,
                "is_agent_configured": is_agent_configured,
            },
        )
        return None
    runner = AutoDiagnosisRunner(
        build_diagnosis_service_opener(settings, http_client),
        settings.diagnosis_auto_start_interval_seconds,
    )
    runner.start()
    return runner


OPENAPI_TAGS = [
    {
        "name": "repair",
        "description": "실패한 배포의 AI 코드 수정 후보·artifact 조회와 서비스용 GitHub 쓰기 인증",
    },
    {"name": "observability", "description": "서비스 런타임 로그·메트릭·SSE"},
    {"name": "auth", "description": "GitHub 로그인·로그아웃, CLI 로그인 세션(생성·승인·폴링)"},
    {"name": "user", "description": "현재 사용자"},
    {"name": "github", "description": "GitHub App 설치와 저장소·브랜치 조회"},
    {"name": "projects", "description": "서비스를 묶는 프로젝트"},
    {"name": "services", "description": "저장소와 연결된 서비스(사용자 앱)"},
    {"name": "deployments", "description": "서비스의 배포 요청 생성·목록·상세(상태 이력)"},
    {
        "name": "uploads",
        "description": "`likelion up` 이 올리는 로컬 소스 아카이브. `CLI` 배포 요청으로 배포한다",
    },
    {
        "name": "diagnosis",
        "description": "실패한 배포를 AI 에이전트로 진단해 원인·해결책을 받는다",
    },
    {"name": "targets", "description": "배포 타깃(aws · local)"},
    {"name": "domains", "description": "서비스가 타깃별로 열리는 공개 도메인 발급·조회"},
    {
        "name": "variables",
        "description": "서비스 환경변수 CRUD·Raw(.env) 일괄 저장, 플랫폼이 자동 주입하는 변수 조회",
    },
    {"name": "webhooks", "description": "외부 서비스(GitHub)가 호출하는 웹훅. 서명으로 인증한다"},
]

app = FastAPI(
    title="AnyDeploy Control API",
    description=(
        "모든 JSON 응답은 `ApiResponse` 봉투(`success`·`code`·`message`·`data`)로 감싼다. "
        "필드는 camelCase 다. 인증은 쿠키(웹) 또는 `Authorization: Bearer`(CLI)."
    ),
    servers=[
        {"url": "/", "description": "현재 API 서버 (개발·운영 공통)"},
        {"url": "https://api.likelion.uk", "description": "운영 Control API"},
    ],
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
# 마지막에 추가한 미들웨어가 가장 바깥이다. 500 응답과 preflight 에도 CORS 헤더가 붙어야 한다.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=get_settings().cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)
register_exception_handlers(app)
app.include_router(auth_router.router)
app.include_router(cli_login_router.router)
app.include_router(observability_router.router)
app.include_router(scaling_router.router)
app.include_router(user_router.router)
app.include_router(github_router.router)
app.include_router(project_router.router)
app.include_router(service_router.router)
app.include_router(deployment_router.router)
app.include_router(upload_router.router)
app.include_router(diagnosis_router.router)
app.include_router(repair_router.router)
app.include_router(target_router.router)
app.include_router(domain_router.router)
app.include_router(variable_router.router)
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
