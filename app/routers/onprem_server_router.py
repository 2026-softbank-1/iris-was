from fastapi import APIRouter, Request, Response, status

from app.dependencies import (
    CurrentUserDep,
    OnpremInstallBaseUrlDep,
    OnpremInstallScriptPathDep,
    OnpremServerSecretDep,
    OnpremServerServiceDep,
)
from app.schemas.onprem_server import (
    BootstrapOnpremServerRequest,
    ConnectOnpremServerRequest,
    CreateOnpremServerRequest,
    OnpremBootstrapResponse,
    OnpremConnectResponse,
    OnpremServerRegistrationResponse,
    OnpremServerResponse,
    RegistryCredentialsResponse,
)
from app.schemas.response import ApiResponse, error_responses
from app.services.onprem_server_service import OnpremServerRegistration, load_install_script

router = APIRouter(prefix="/api/v1/onprem-servers", tags=["onprem-servers"])

# 설치 스크립트의 `--api-url` 기본값. 이 주소면 명령에서 생략한다.
DEFAULT_API_URL = "https://api.likelion.uk"


def _build_install_command(request: Request, base_url: str, token: str) -> str:
    path = request.app.url_path_for("get_onprem_install_script")
    command = f"curl -fsSL {base_url}{path} | sudo bash -s -- --token {token}"
    if base_url != DEFAULT_API_URL:
        command += f" --api-url {base_url}"
    return command


def _registration_response(
    request: Request, base_url: str, registration: OnpremServerRegistration
) -> OnpremServerRegistrationResponse:
    command = _build_install_command(request, base_url, registration.registration_token)
    return OnpremServerRegistrationResponse.from_registration(registration, command)


# --- 서버 쪽 API (사용자 인증 없음). `/{server_id}` 보다 먼저 등록한다.


@router.get(
    "/install.sh",
    response_class=Response,
    summary="온프레미스 서버 설치 스크립트",
    responses={
        200: {"content": {"text/x-shellscript": {}}, "description": "설치 스크립트 원문"},
        **error_responses(503),
    },
)
async def get_onprem_install_script(path: OnpremInstallScriptPathDep) -> Response:
    """인증 없이 받는다. `installCommand` 가 이 주소를 `curl | sudo bash` 로 실행한다."""
    script = await load_install_script(path)
    return Response(content=script, media_type="text/x-shellscript")


@router.post(
    "/bootstrap",
    response_model=ApiResponse[OnpremBootstrapResponse],
    response_model_exclude_none=True,
    summary="서버 설치 시작 (등록 토큰으로 설정 받기)",
    responses=error_responses(401, 422, 503),
)
async def bootstrap_onprem_server(
    body: BootstrapOnpremServerRequest, service: OnpremServerServiceDep
) -> ApiResponse[OnpremBootstrapResponse]:
    """설치 스크립트가 부른다. 서버가 `PENDING`·`REGISTERING`·`FAILED` 이고 토큰이 만료 전일 때만
    답하며 상태는 바꾸지 않는다(설치 재실행).
    토큰이 없거나 만료됐거나 이미 연결된 서버면 구분하지 않고 401 `INVALID_REGISTRATION_TOKEN` 이다.
    """
    bootstrap = await service.bootstrap(body.registration_token)
    return ApiResponse(data=OnpremBootstrapResponse.from_bootstrap(bootstrap))


@router.post(
    "/connect",
    response_model=ApiResponse[OnpremConnectResponse],
    response_model_exclude_none=True,
    summary="서버 클러스터 접속 정보 등록",
    responses=error_responses(401, 422, 503),
)
async def connect_onprem_server(
    body: ConnectOnpremServerRequest, service: OnpremServerServiceDep
) -> ApiResponse[OnpremConnectResponse]:
    """설치 스크립트가 클러스터를 띄운 뒤 부른다. 상태는 `REGISTERING` 이 되고 Deploy Worker 가
    GitOps 에 반영해 연결을 확인한다. 같은 토큰으로 다시 보내면 값을 덮어쓰고 새 `serverSecret` 을
    준다.
    """
    connection = await service.connect(
        body.registration_token,
        tailnet_fqdn=body.tailnet_fqdn,
        api_ca_cert=body.api_ca_cert,
        service_account_token=body.service_account_token,
        sealed_secrets_cert=body.sealed_secrets_cert,
    )
    return ApiResponse(data=OnpremConnectResponse.from_connection(connection))


@router.post(
    "/registry-credentials",
    response_model=ApiResponse[RegistryCredentialsResponse],
    response_model_exclude_none=True,
    summary="서버용 ECR pull 자격증명",
    responses=error_responses(401, 409, 502, 503),
)
async def issue_onprem_registry_credentials(
    server_secret: OnpremServerSecretDep, service: OnpremServerServiceDep
) -> ApiResponse[RegistryCredentialsResponse]:
    """서버의 CronJob 이 `Authorization: Bearer <serverSecret>` 로 부른다. 비밀이 틀리면 401,
    아직 `CONNECTED` 가 아니면 409 `ONPREM_SERVER_NOT_CONNECTED` 다.
    이 서버 타깃에 붙은 서비스의 ECR 저장소만 받을 수 있다. 붙은 서비스가 없으면 password 가 없다.
    """
    credentials = await service.issue_registry_credentials(server_secret)
    return ApiResponse(data=RegistryCredentialsResponse.from_credentials(credentials))


# --- 사용자 API


@router.post(
    "",
    response_model=ApiResponse[OnpremServerRegistrationResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="온프레미스 서버 등록",
    responses=error_responses(401, 409, 422, 503),
)
async def create_onprem_server(
    request: Request,
    body: CreateOnpremServerRequest,
    user: CurrentUserDep,
    service: OnpremServerServiceDep,
    base_url: OnpremInstallBaseUrlDep,
) -> ApiResponse[OnpremServerRegistrationResponse]:
    """서버와 전용 배포 타깃을 만든다. 응답의 `installCommand` 를 서버에서 실행하면 연결된다.
    `registrationToken`(24시간)은 이 응답에서만 보인다. 사용자마다 5대까지다(넘으면 409
    `ONPREM_SERVER_LIMIT_EXCEEDED`). `API_BASE_URL` 이 없거나 https 가 아니면 503 이다.
    """
    registration = await service.create_server(user.id, body.name)
    return ApiResponse(data=_registration_response(request, base_url, registration))


@router.get(
    "",
    response_model=ApiResponse[list[OnpremServerResponse]],
    response_model_exclude_none=True,
    summary="내 온프레미스 서버 목록",
    responses=error_responses(401),
)
async def search_onprem_servers(
    user: CurrentUserDep, service: OnpremServerServiceDep
) -> ApiResponse[list[OnpremServerResponse]]:
    """최근 등록한 순서다."""
    servers = await service.search_servers(user.id)
    return ApiResponse(data=[OnpremServerResponse.from_model(s) for s in servers])


@router.get(
    "/{server_id}",
    response_model=ApiResponse[OnpremServerResponse],
    response_model_exclude_none=True,
    summary="온프레미스 서버 조회",
    responses=error_responses(401, 404, 422),
)
async def get_onprem_server(
    server_id: int, user: CurrentUserDep, service: OnpremServerServiceDep
) -> ApiResponse[OnpremServerResponse]:
    """연결 상태(`status`)를 확인할 때 쓴다. 남의 서버는 404 다."""
    server = await service.get_server(user.id, server_id)
    return ApiResponse(data=OnpremServerResponse.from_model(server))


@router.post(
    "/{server_id}/registration-token",
    response_model=ApiResponse[OnpremServerRegistrationResponse],
    response_model_exclude_none=True,
    summary="등록 토큰 재발급",
    responses=error_responses(401, 404, 409, 422, 503),
)
async def reissue_onprem_registration_token(
    request: Request,
    server_id: int,
    user: CurrentUserDep,
    service: OnpremServerServiceDep,
    base_url: OnpremInstallBaseUrlDep,
) -> ApiResponse[OnpremServerRegistrationResponse]:
    """`PENDING`·`REGISTERING`·`FAILED` 일 때만(`CONNECTED` 는 409 `INVALID_STATUS_TRANSITION`).
    이전 토큰·서버 비밀은 무효가 되고 상태는 `PENDING` 이다. 진행 중이던 연결 확인은 멈춘다.
    새 `installCommand` 를 서버에서 다시 실행한다.
    """
    registration = await service.reissue_registration_token(user.id, server_id)
    return ApiResponse(data=_registration_response(request, base_url, registration))


@router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="온프레미스 서버 삭제",
    responses=error_responses(401, 404, 409, 422),
)
async def delete_onprem_server(
    server_id: int, user: CurrentUserDep, service: OnpremServerServiceDep
) -> Response:
    """서버와 타깃을 지운다. 서비스가 붙어 있으면 409 `ONPREM_SERVER_IN_USE` 다. 서버의 클러스터는
    건드리지 않고, platform 의 연결 정보만 Deploy Worker 가 GitOps 에서 지운다.
    """
    await service.delete_server(user.id, server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
