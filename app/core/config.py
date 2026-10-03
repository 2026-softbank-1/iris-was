from functools import lru_cache
from typing import Literal

from pydantic import HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 소스 스냅샷·업로드 아카이브(압축한 바이트)의 한도 기본값. Control API·Build Worker 가 같게 쓴다.
DEFAULT_SNAPSHOT_MAX_BYTES = 250 * 1024 * 1024


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
    snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    # 업로드를 풀었을 때의 총 크기·항목 수 한도(압축 폭탄 방어). 압축 크기 한도와 따로 둔다.
    upload_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    upload_max_entries: int = 100_000
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
    # 서비스 환경변수 스냅샷(암호문)을 푸는 키. Control API 와 같은 값이다.
    # 변수가 있는 배포를 GitOps 에 쓸 때만 필요하고, 변수가 있는데 없으면 그 배포는 실패한다.
    variables_encryption_key: SecretStr | None = None
    # workload 의 Sealed Secrets controller 공개 인증서(PEM, 비밀이 아니다). 변수를 봉인할 때 쓴다.
    sealed_secrets_cert: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_build_worker_settings() -> BuildWorkerSettings:
    return BuildWorkerSettings()


@lru_cache
def get_deploy_worker_settings() -> DeployWorkerSettings:
    return DeployWorkerSettings()
