import asyncio
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import get_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

configure_logging("control-api", get_settings().log_level)
logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0

app = FastAPI(title="AnyDeploy Control API")
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)


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
