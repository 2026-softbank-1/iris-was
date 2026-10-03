from functools import lru_cache
from typing import Literal

from pydantic import HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 로그·메트릭 백엔드(management 클러스터 내부). 없으면 관측 API 는 503 (NOT_CONFIGURED).
    # ponytail: 모든 target 이 같은 백엔드를 쓴다(수집 대상은 AWS workload 클러스터 하나).
    # 클러스터가 늘면 target 별 주소와 k8s_cluster_name selector 로 나눈다.
    loki_url: HttpUrl | None = None
    prometheus_url: HttpUrl | None = None

    # 에러 진단 에이전트 서버(iris-error-check-agent). 둘 중 하나라도 없으면 진단 API 는 503.
    diagnosis_agent_url: HttpUrl | None = None
    diagnosis_agent_api_key: SecretStr | None = None
    # 에이전트는 모델을 최대 2번 부른다(호출마다 60초). 그보다 길게 기다린다.
    diagnosis_agent_timeout_seconds: float = 150.0
    # 진단에 소스를 함께 넘기려면 빌드가 스냅샷을 올린 버킷을 읽는 권한(S3 GetObject)이 필요하다.
    # 둘 다 있어야 소스를 보내고, 없으면 로그만 진단한다.
    aws_region: str | None = None
    artifact_bucket: str | None = None

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
    snapshot_max_bytes: int = 250 * 1024 * 1024
    poll_interval_seconds: float = 10.0


class DeployWorkerSettings(BaseSettings):
    """Deploy Worker 전용. Build Worker 와 GitHub App·자격증명을 공유하지 않는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str
    # 사용자 서비스 도메인. Control Plane 과 다른 등록 도메인이다.
    base_domain: str
    gitops_repository: str  # {owner}/{repo}
    gitops_app_id: int
    gitops_app_private_key: SecretStr
    gitops_installation_id: int
    argocd_server_url: str
    argocd_token: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()


@lru_cache
def get_deploy_worker_settings() -> DeployWorkerSettings:
    return DeployWorkerSettings()
