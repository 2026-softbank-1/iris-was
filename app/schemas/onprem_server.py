from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from app.enums import OnpremServerFailureCode, OnpremServerStatus
from app.models.onprem_server import OnpremServer
from app.schemas.response import ApiModel
from app.services.onprem_server_service import (
    OnpremBootstrap,
    OnpremConnection,
    OnpremServerRegistration,
    RegistryCredentials,
)

ServerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=63)]
SecretText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
PemText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16384)]


class CreateOnpremServerRequest(ApiModel):
    name: ServerName = Field(
        description="내 서버 안에서 유일한 이름(1~63자)", examples=["home-lab"]
    )


class OnpremServerResponse(ApiModel):
    id: int
    name: str
    server_key: str = Field(
        description="서버를 가리키는 8자 키. 타깃·도메인 이름의 기준이다(비밀 아님)",
        examples=["k3x9q2ma"],
    )
    status: OnpremServerStatus = Field(
        description=(
            "PENDING 명령 실행 전 · REGISTERING 연결 확인 중 · CONNECTED 배포 가능 · "
            "FAILED 연결 실패(토큰 재발급 후 다시 실행)"
        )
    )
    target_id: int = Field(description="이 서버 전용 배포 타깃. 서비스의 targetIds 로 쓴다")
    tailnet_fqdn: str | None = Field(default=None, examples=["iris-k3x9q2ma.tailb046e8.ts.net"])
    failure_code: OnpremServerFailureCode | None = Field(
        default=None,
        description="FAILED 일 때만. CONNECT_TIMED_OUT · GITOPS_COMMIT_FAILED",
    )
    registration_expires_at: datetime = Field(description="등록 토큰 만료 시각")
    connected_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_model(cls, server: OnpremServer) -> "OnpremServerResponse":
        return cls(
            id=server.id,
            name=server.name,
            server_key=server.server_key,
            status=server.status,
            target_id=server.target_id,
            tailnet_fqdn=server.tailnet_fqdn,
            failure_code=server.failure_code,
            registration_expires_at=server.registration_expires_at,
            connected_at=server.connected_at,
            created_at=server.created_at,
        )


class OnpremServerRegistrationResponse(ApiModel):
    server: OnpremServerResponse
    registration_token: str = Field(
        description="서버 설치 명령에 쓰는 1회용 토큰(24시간). 이 응답 말고는 다시 볼 수 없다",
        examples=["Zk3v1u0eQ8mY2nXr5oPq7sT9wUa4bC6dEfGhIjKlMnO"],
    )
    install_command: str = Field(
        description="서버(Ubuntu)에서 root 로 실행할 명령. 등록 토큰을 담고 있다",
        examples=[
            "curl -fsSL https://api.likelion.uk/api/v1/onprem-servers/install.sh"
            " | sudo bash -s -- --token <registrationToken>"
        ],
    )

    @classmethod
    def from_registration(
        cls, registration: OnpremServerRegistration, install_command: str
    ) -> "OnpremServerRegistrationResponse":
        return cls(
            server=OnpremServerResponse.from_model(registration.server),
            registration_token=registration.registration_token,
            install_command=install_command,
        )


class BootstrapOnpremServerRequest(ApiModel):
    registration_token: SecretText = Field(description="등록·재발급 응답의 registrationToken")


class OnpremTailscaleResponse(ApiModel):
    auth_key: str = Field(description="tailnet 가입 키. 출력·로그에 남기지 않는다")
    hostname: str = Field(examples=["iris-k3x9q2ma"])
    tags: list[str] = Field(examples=[["tag:iris-onprem"]])


class OnpremVersionsResponse(ApiModel):
    # 자동 camelCase 는 `k3S` 가 되어 계약 이름을 직접 준다.
    k3s: str = Field(alias="k3s", examples=["v1.31.4+k3s1"])
    argo_rollouts: str = Field(examples=["v1.7.2"])
    sealed_secrets: str = Field(examples=["0.27.1"])


class OnpremBootstrapResponse(ApiModel):
    server_key: str = Field(examples=["k3x9q2ma"])
    tailscale: OnpremTailscaleResponse
    versions: OnpremVersionsResponse

    @classmethod
    def from_bootstrap(cls, bootstrap: OnpremBootstrap) -> "OnpremBootstrapResponse":
        return cls(
            server_key=bootstrap.server_key,
            tailscale=OnpremTailscaleResponse(
                auth_key=bootstrap.tailscale_auth_key,
                hostname=bootstrap.tailscale_hostname,
                tags=list(bootstrap.tailscale_tags),
            ),
            versions=OnpremVersionsResponse(
                k3s=bootstrap.k3s_version,
                argo_rollouts=bootstrap.argo_rollouts_version,
                sealed_secrets=bootstrap.sealed_secrets_version,
            ),
        )


class ConnectOnpremServerRequest(ApiModel):
    registration_token: SecretText
    tailnet_fqdn: str = Field(
        min_length=1,
        max_length=253,
        description="`iris-{serverKey}.` 로 시작하는 tailnet FQDN",
        examples=["iris-k3x9q2ma.tailb046e8.ts.net"],
    )
    api_ca_cert: PemText = Field(description="K3s API server CA (PEM)")
    service_account_token: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)
    ] = Field(description="Argo CD 가 쓸 만료 없는 ServiceAccount 토큰. 암호화해 저장한다")
    sealed_secrets_cert: PemText = Field(
        description="서버 Sealed Secrets controller 공개 인증서 (PEM)"
    )


class OnpremConnectResponse(ApiModel):
    status: OnpremServerStatus = Field(examples=["REGISTERING"])
    server_secret: str = Field(
        description="ECR 자격증명을 받을 때 Bearer 로 보내는 비밀. 이 응답 말고는 다시 볼 수 없다"
    )

    @classmethod
    def from_connection(cls, connection: OnpremConnection) -> "OnpremConnectResponse":
        return cls(status=connection.status, server_secret=connection.server_secret)


class RegistryCredentialsResponse(ApiModel):
    registry: str = Field(examples=["123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"])
    username: str = Field(examples=["AWS"])
    password: str | None = Field(default=None, description="ECR 토큰. 붙은 서비스가 없으면 없다")
    expires_at: datetime | None = Field(default=None, description="password 만료 시각")
    service_ids: list[int] = Field(
        description="이 자격증명으로 이미지를 받을 수 있는 서비스", examples=[[12, 15]]
    )

    @classmethod
    def from_credentials(cls, credentials: RegistryCredentials) -> "RegistryCredentialsResponse":
        return cls(
            registry=credentials.registry,
            username=credentials.username,
            password=credentials.password,
            expires_at=credentials.expires_at,
            service_ids=credentials.service_ids,
        )
