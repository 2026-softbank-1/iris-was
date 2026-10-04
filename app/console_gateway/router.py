"""Console Gateway 의 REST·WebSocket 엔드포인트. 로직 없이 서비스를 부르기만 한다."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection

from app.console_gateway.transport import WebSocketTransport
from app.core.exceptions import UnauthorizedError
from app.schemas.console import ConsolePodResponse, ConsolePodsResponse
from app.schemas.response import ApiResponse
from app.services.console_gateway_service import ConsoleGatewayService

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)
# 브라우저가 연결 거절(HTTP 403)로 보는 정책 위반 종료 코드.
_WS_POLICY_VIOLATION = 1008


def get_console_gateway_service(connection: HTTPConnection) -> ConsoleGatewayService:
    service: ConsoleGatewayService = connection.app.state.console_gateway_service
    return service


GatewayServiceDep = Annotated[ConsoleGatewayService, Depends(get_console_gateway_service)]


@router.get(
    "/v1/pods", response_model=ApiResponse[ConsolePodsResponse], response_model_exclude_none=True
)
async def search_pods(
    service: GatewayServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiResponse[ConsolePodsResponse]:
    if credentials is None:
        raise UnauthorizedError("console ticket required")
    pods = await service.search_pods(credentials.credentials)
    return ApiResponse(
        data=ConsolePodsResponse(pods=[ConsolePodResponse.from_info(pod) for pod in pods])
    )


@router.websocket("/v1/exec")
async def exec_console(
    websocket: WebSocket,
    service: GatewayServiceDep,
    pod: Annotated[str, Query(min_length=1, max_length=253)],
) -> None:
    allowed_origins: frozenset[str] = websocket.app.state.console_allowed_origins
    if websocket.headers.get("origin") not in allowed_origins:
        # accept 하기 전에 닫으면 업그레이드가 HTTP 403 으로 거절된다.
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    transport = WebSocketTransport(websocket)
    await service.serve_exec(transport, pod)
    await transport.close()
