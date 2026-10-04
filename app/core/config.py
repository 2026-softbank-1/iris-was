import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, WebsocketUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 소스 스냅샷·업로드 아카이브(압축한 바이트)의 한도 기본값. Control API·Build Worker 가 같게 쓴다.
DEFAULT_SNAPSHOT_MAX_BYTES = 250 * 1024 * 1024
# 하트비트가 끊긴 CONNECTED 서버를 DISCONNECTED 로 보는 기본 기준(초).
DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS = 180
# iris-infra 가 서비스 외부 트래픽 지표에 붙이는 workload 클러스터 라벨 값(dev).
DEFAULT_TRAFFIC_CLUSTER = "iris-dev-workload"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 로그·메트릭 백엔드(management 클러스터 내부). 없으면 관측 API 는 503 (NOT_CONFIGURED).
    # ponytail: 모든 target 이 같은 백엔드를 쓴다(수집 대상은 AWS workload 클러스터 하나).
    # 클러스터가 늘면 target 별 주소와 k8s_cluster_name selector 로 나눈다.
    loki_url: HttpUrl | None = None
    prometheus_url: HttpUrl | None = None
    # 요청 수·오류율·응답 시간·공용 네트워크 지표를 고르는 `cluster` 라벨 값.
    traffic_cluster: str = DEFAULT_TRAFFIC_CLUSTER
    # on-prem 타깃(공용 `onprem`·사용자 등록 서버)의 런타임 로그는 Loki 에 없어 Argo CD 의 Pod
    # 로그 API 로 읽는다(ADR 0034). 읽기 전용 토큰은 Argo project role `iris-log-reader`
    # (`iris-svc-project`, applications get·logs get)다. 둘 중 하나라도 없으면 on-prem 로그는
    # 503 (NOT_CONFIGURED). Deploy Worker 의 ARGOCD_TOKEN 과 공유하지 않는다.
    argocd_server_url: str | None = None
    argocd_logs_token: SecretStr | None = None

    # 에러 진단 에이전트 서버(iris-error-check-agent). 둘 중 하나라도 없으면 진단 API 는 503.
    diagnosis_agent_url: HttpUrl | None = None
    diagnosis_agent_api_key: SecretStr | None = None
    # 에이전트는 모델을 최대 2번 부른다(호출마다 60초). 그보다 길게 기다린다.
    diagnosis_agent_timeout_seconds: float = 150.0
    # Candidate generation only; repository publication and deployment remain separate.
    repair_agent_url: HttpUrl | None = None
    repair_agent_api_key: SecretStr | None = None
    repair_agent_timeout_seconds: float = Field(default=150, gt=0, allow_inf_nan=False)
    repair_agent_deadline_seconds: float = Field(default=240, gt=0, le=1800, allow_inf_nan=False)
    repair_agent_max_cost_usd: float = Field(default=1, gt=0, allow_inf_nan=False)
    repair_agent_source_hosts: str = ""

    # 실패가 확정된 배포를 서버가 자동으로 진단한다(에이전트가 설정돼 있어야 한다). 모델 비용이
    # 실패마다 들어 끄고 싶으면 false 로 둔다. 끄면 사용자가 버튼으로 시작하는 진단만 남는다.
    diagnosis_auto_start_enabled: bool = True
    # 진단할 배포를 찾는 주기. 실패가 확정된 뒤 진단이 시작되기까지 걸리는 시간의 상한이다.
    diagnosis_auto_start_interval_seconds: float = 5.0
    # 빌드 입력(소스 스냅샷·업로드)을 두는 S3 버킷. 둘 다 있어야 쓴다.
    # - 진단에 소스를 함께 넘기려면 스냅샷(`snapshots/`)을 읽는 권한(S3 GetObject)이 필요하다.
    #   없으면 로그만 진단한다.
    # - 소스 업로드 API(`likelion up`)는 `uploads/` 에 쓰는 권한(S3 PutObject)이 필요하다.
    #   없으면 업로드 API 는 503 (NOT_CONFIGURED).
    aws_region: str | None = None
    artifact_bucket: str | None = None
    # 배포 상세의 빌드 로그 전체(CodeBuild → CloudWatch Logs) 읽기 전용 조회. AWS_REGION 과
    # 둘 다 있어야 한다. 없으면 Build Worker 가 남긴 실패한 빌드의 끝부분(builds.log_tail)만
    # 보여 준다. 그룹은 iris-infra foundation 의 CodeBuild 로그 그룹이다(dev:
    # /aws/codebuild/iris-dev-build). Control API Role 에 그 그룹의 logs:GetLogEvents 가 필요하다.
    build_log_group: str | None = None
    # 소스 업로드의 압축한 바이트 한도. Build Worker 의 snapshot_max_bytes 와 같게 둔다.
    upload_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    # 카나리·블루그린 배포 방식. Deploy Worker 와 같은 값으로 둔다. 꺼져 있으면 두 방식을 저장할
    # 수 없고 새 배포 요청은 ROLLING 으로 적용한다. iris-service chart 0.7.0 이 배포된 뒤에 켠다.
    deployment_strategy_enabled: bool = False
    # 관리형 DB·호스트 별칭·프로젝트 내부 통신(chart 0.9.0 의 workload·database·hostAliases·
    # projectId). Deploy Worker 와 같은 값으로 둔다. 꺼져 있으면 DB 생성·별칭 저장이 422 이고
    # apply 는 DB 를 만들지 않는다. AWS ApplicationSet 의 iris-service chart pin 이 0.9.0 이상이
    # 된 뒤에 켠다. on-prem 타깃(공용·사용자 등록 서버)은 이 기능을 쓰지 않는다.
    project_networking_enabled: bool = False
    # 관리형 DB 의 고정 이미지(엔진 → `repo:tag@sha256:…`). 비우면 코드의 기본 digest 를 쓴다.
    database_images: dict[str, str] = Field(default_factory=dict)

    # 사용자 온프레미스 서버 등록(ADR 0029).
    # 서버가 tailnet 에 가입할 때 쓰는 Tailscale 가입 키(reusable·pre-approved·tag:iris-onprem).
    # 없으면 bootstrap API 는 503 (NOT_CONFIGURED). 화면·CLI·로그에 내보내지 않는다.
    onprem_tailscale_auth_key: SecretStr | None = None
    # 서버에 ECR pull 자격증명을 줄 때 AssumeRole 하는 Role(iris-infra 의 ECR pull 전용 Role).
    # AWS_REGION 과 둘 다 있어야 한다. 없으면 registry-credentials API 는 503 (NOT_CONFIGURED).
    onprem_ecr_pull_role_arn: str | None = None
    # AssumeRole 세션 길이(초). Control API 자격증명이 이미 role 세션이라 AssumeRole 이 연쇄되어
    # AWS 가 1시간까지만 허용한다. 서버의 CronJob 이 5분마다 갱신하므로 충분하다.
    onprem_ecr_pull_session_seconds: int = Field(default=3600, ge=900, le=3600)
    # 설치 스크립트가 서버에 고정해 설치하는 버전. bootstrap 응답으로 내려간다. iris-infra 가
    # 고정한 버전(Argo Rollouts `helm/versions.json`, Sealed Secrets chart 의 appVersion)과 맞춘다.
    onprem_k3s_version: str = "v1.33.13+k3s2"
    onprem_argo_rollouts_version: str = "v1.10.0"
    onprem_sealed_secrets_version: str = "0.40.0"
    # CONNECTED 서버의 하트비트가 이만큼(초) 없으면 API 가 DISCONNECTED 로 알리고 배포를 막는다.
    # 서버 CronJob 이 1분마다 부르므로 두 번 넘게 놓친 뒤다.
    onprem_server_offline_after_seconds: int = Field(
        default=DEFAULT_ONPREM_SERVER_OFFLINE_AFTER_SECONDS, gt=0
    )

    # 서비스 콘솔(Pod 셸, ADR 0033). Control API 는 ticket 을 서명하고 Console Gateway 주소를
    # 알려 줄 뿐이고 클러스터에는 닿지 않는다. 셋 중 하나라도 없으면 콘솔은 꺼진다(가능 여부
    # 조회는 `NOT_CONFIGURED`, ticket 발급은 503). 개인키는 Ed25519 PEM 이고 Gateway 에는
    # 공개키만 둔다.
    console_ticket_private_key: SecretStr | None = None
    console_gateway_http_url: HttpUrl | None = None
    console_gateway_ws_url: WebsocketUrl | None = None

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # 웹 프런트엔드 주소. 로그인이 끝나면 여기로 돌려보낸다.
    web_base_url: str = "http://localhost:3000"
    # Control API 의 공개 주소(예: https://api.likelion.uk). CLI 로그인의 verificationUrl 을 만든다.
    # 없으면 요청의 Host 로 만든다. TLS 를 앞단에서 끝내는 운영에서는 https 주소로 꼭 지정한다.
    api_base_url: str | None = None
    # CORS 로 허용할 Origin 정규식(전체 일치). 쿠키 인증이라 `*` 대신 Origin 을 되돌려 줘야 한다.
    # 기본값: likelion.uk 와 모든 하위 도메인(https), localhost·127.0.0.1 의 모든 포트.
    cors_allow_origin_regex: str = (
        r"https://([a-z0-9-]+\.)*likelion\.uk|https?://(localhost|127\.0\.0\.1)(:\d+)?"
    )

    # 세션. 값이 없으면 로그인 API 는 503 (NOT_CONFIGURED) 을 돌려준다.
    session_secret: SecretStr | None = None
    session_ttl_minutes: int = 60 * 24 * 7
    session_cookie_name: str = "anydeploy_session"
    # 로컬 http 개발에서는 false 로 둔다. 운영은 반드시 true.
    is_session_cookie_secure: bool = True

    # 서비스 환경변수 값을 DB 에 암호화해 저장하는 Fernet 키(README 의 생성 명령 참고).
    # 없으면 변수 API 는 503 (NOT_CONFIGURED). 키를 잃으면 저장된 값을 읽을 수 없다.
    variables_encryption_key: SecretStr | None = None

    # GitHub App 하나로 로그인(user authorization)과 저장소 접근(installation)을 함께 쓴다.
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    # PEM 전체. 환경변수에는 줄바꿈을 `\n` 두 글자로 적어도 된다.
    github_app_private_key: SecretStr | None = None
    # 웹훅 서명(X-Hub-Signature-256) 검증용. 값이 없으면 웹훅 API 는 503 (NOT_CONFIGURED) 이다.
    github_webhook_secret: SecretStr | None = None
    github_web_base_url: str = "https://github.com"
    github_api_base_url: str = "https://api.github.com"


class ConsoleGatewaySettings(BaseSettings):
    """Console Gateway 전용. DB 접속 정보와 Control API 의 서명 개인키를 갖지 않는다(ADR 0033).

    클러스터는 둘이고 각각 선택이다(ADR 0035). 하나만 설정해도 Gateway 는 뜨고, 설정하지 않은
    클러스터의 ticket 은 `CLUSTER_UNAVAILABLE` 이다. 한 그룹의 값이 일부만 있거나 둘 다 없으면
    시작하지 않는다.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ticket 검증용 Ed25519 공개키(PEM, 비밀이 아니다). Control API 의 개인키와 한 쌍이다.
    console_ticket_public_key: str
    # AWS 타깃: 접속할 Prod EKS. 토큰은 EKS Pod Identity 자격증명으로 만든
    # sts:GetCallerIdentity presigned URL 이다. 네 값이 모두 있어야 켜진다.
    console_aws_cluster_name: str | None = None
    console_aws_cluster_endpoint: HttpUrl | None = None
    # 클러스터 API 서버 인증서의 CA(PEM 을 base64 로 인코딩한 값, EKS 가 주는 형식).
    console_aws_cluster_ca: str | None = None
    aws_region: str | None = None
    # ONPREM 타깃: Argo CD(`argocd-server`)의 주소와 프로젝트 role 토큰. 둘 다 있어야 켜진다.
    # 토큰은 role `iris-console`(applications get · exec create)의 JWT 다.
    console_argocd_server_url: HttpUrl | None = None
    console_argocd_token: SecretStr | None = None
    # 서비스 Application 이 속한 Argo CD project 와 Application 의 namespace.
    console_argocd_project: str = "iris-svc-project"
    console_argocd_app_namespace: str = "argocd"
    # CORS·WebSocket 에 허용할 Origin(쉼표로 구분). 예: https://app.likelion.uk
    console_allowed_origins: str
    # 입력 없이 이 시간이 지나면 끊는다. ping·pong 은 입력이 아니다.
    console_idle_timeout_seconds: float = Field(default=900, gt=0)
    # 연결한 뒤 이 시간이 지나면 끊는다.
    console_max_session_seconds: float = Field(default=3600, gt=0)
    # 한 사용자의 동시 연결 한도. Gateway replica 안에서만 센다.
    console_max_sessions_per_user: int = Field(default=3, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("console_aws_cluster_endpoint", "console_argocd_server_url")
    @classmethod
    def _require_https_endpoint(cls, value: HttpUrl | None) -> HttpUrl | None:
        # bearer 토큰·쿠키를 평문으로 보내지 않는다.
        if value is not None and value.scheme != "https":
            raise ValueError("cluster endpoint must be https")
        return value

    @model_validator(mode="after")
    def _require_complete_cluster_groups(self) -> "ConsoleGatewaySettings":
        # 오류 메시지에는 변수 이름만 담는다(값에 비밀이 있을 수 있다).
        aws = {
            "CONSOLE_AWS_CLUSTER_NAME": self.console_aws_cluster_name,
            "CONSOLE_AWS_CLUSTER_ENDPOINT": self.console_aws_cluster_endpoint,
            "CONSOLE_AWS_CLUSTER_CA": self.console_aws_cluster_ca,
            "AWS_REGION": self.aws_region,
        }
        argocd = {
            "CONSOLE_ARGOCD_SERVER_URL": self.console_argocd_server_url,
            "CONSOLE_ARGOCD_TOKEN": self.console_argocd_token,
        }
        for group in (aws, argocd):
            missing = [name for name, value in group.items() if value is None]
            if missing and len(missing) < len(group):
                raise ValueError(f"cluster settings are incomplete: missing {', '.join(missing)}")
        if not self.is_aws_configured and not self.is_onprem_configured:
            raise ValueError(
                "no console cluster is configured: set the AWS cluster settings "
                "(CONSOLE_AWS_CLUSTER_*, AWS_REGION) or the Argo CD settings (CONSOLE_ARGOCD_*)"
            )
        return self

    @property
    def is_aws_configured(self) -> bool:
        return self.console_aws_cluster_endpoint is not None

    @property
    def is_onprem_configured(self) -> bool:
        return self.console_argocd_server_url is not None

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.console_allowed_origins.split(",") if origin.strip()
        ]


class BuildWorkerSettings(BaseSettings):
    """Build Worker 전용. Control API 는 이 값(GitHub App 키·AWS 리소스)을 갖지 않는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_app_id: int
    github_app_private_key: SecretStr
    # 앱을 설치하지 않은 공개 레포는 우리 조직 설치의 토큰으로 받는다.
    aws_region: str
    # iris-infra foundation stack 출력값(build_codebuild_project_name·build_artifact_bucket_name)
    codebuild_project: str
    artifact_bucket: str

    concurrency: int = 4
    user_concurrent_build_limit: int = 2
    build_timeout_minutes: int = 15
    snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    # 업로드를 풀었을 때의 총 크기·항목 수 한도(압축 폭탄 방어). 압축 크기 한도와 따로 둔다.
    upload_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    upload_max_entries: int = 100_000
    poll_interval_seconds: float = 10.0
    # 레포 구성 분석(Analysis Gate) 분석기 명령. JSON 배열(argv)로 적는다. 기본은 이미지에 설치된
    # iris-analyzer wheel 이다. 셸을 거치지 않고, Worker 의 자격증명은 넘기지 않는다.
    analysis_gate_command: list[str] = Field(
        default_factory=lambda: [sys.executable, "-m", "iris_analyzer.gate.cli", "--request-stdin"]
    )
    analysis_gate_timeout_seconds: float = Field(default=120, gt=0, le=600)
    # 동시에 실행하는 분석 수. 빌드 슬롯(concurrency)과 따로 센다.
    analysis_gate_concurrency: int = Field(default=2, ge=1)


class DeployWorkerSettings(BaseSettings):
    """Deploy Worker 전용. Build Worker 와 GitHub App·자격증명을 공유하지 않는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str
    gitops_repository: str  # {owner}/{repo}
    gitops_app_id: int
    gitops_app_private_key: SecretStr
    gitops_installation_id: int
    argocd_server_url: str
    argocd_token: SecretStr
    # 서비스 환경변수 스냅샷(암호문)을 푸는 키. Control API 와 같은 값이다.
    # 변수가 있는 배포를 GitOps 에 쓸 때만 필요하고, 변수가 있는데 없으면 그 배포는 실패한다.
    variables_encryption_key: SecretStr | None = None
    # workload 의 Sealed Secrets controller 공개 인증서(PEM, 비밀이 아니다). 변수를 봉인할 때 쓴다.
    sealed_secrets_cert: str | None = None
    # management 클러스터 Sealed Secrets controller 공개 인증서(PEM, 비밀이 아니다). 사용자가 등록한
    # 서버의 Argo CD cluster 접속 정보를 봉인한다. 없으면 서버 등록을 처리하지 않아 서버가
    # REGISTERING 에 머문다. 서비스 변수용 SEALED_SECRETS_CERT 와 다른 인증서다.
    platform_sealed_secrets_cert: str | None = None
    # 등록한 서버의 probe Application(Argo project `iris-onprem-probe`)을 읽는 토큰. ARGOCD_TOKEN 은
    # 자기 project 만 보므로 따로 받는다(role `iris-deploy-reader`, applications get). 없으면 서버
    # 연결 확인을 하지 않아 서버가 REGISTERING 에 머문다.
    argocd_probe_token: SecretStr | None = None
    # 지운 온프레미스 서버의 tailnet 기기(`iris-{serverKey}`, tag:iris-onprem)를 지우는
    # Tailscale API 키(devices 읽기·쓰기). 없으면 기기를 남기고 경고 로그만 남긴다. 개인 API 키는
    # 최대 90일이라 만료 전에 바꾼다(나중에 OAuth client 로 바꾼다).
    tailscale_api_key: SecretStr | None = None
    # `-` 는 키가 속한 기본 tailnet 이다.
    tailscale_tailnet: str = "-"
    # values 에 deploymentStrategy 를 쓴다. 이 키를 모르는 이전 chart(0.7.0 미만)의 schema 가
    # 거절하므로 chart 0.7.0 이 배포된 뒤에 켠다. Control API 와 같은 값으로 둔다.
    deployment_strategy_enabled: bool = False
    # values 에 projectId·service.exposeContainerPort·hostAliases·workload·database 를 쓴다(AWS
    # 타깃만). 이 키를 모르는 이전 chart(0.9.0 미만)의 schema 가 거절하므로 AWS chart pin 이
    # 0.9.0 이 된 뒤에 켠다. Control API 와 같은 값으로 둔다.
    project_networking_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()


@lru_cache
def get_deploy_worker_settings() -> DeployWorkerSettings:
    return DeployWorkerSettings()


@lru_cache
def get_console_gateway_settings() -> ConsoleGatewaySettings:
    return ConsoleGatewaySettings()
