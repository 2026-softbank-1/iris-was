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

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # 웹 프런트엔드 주소. 로그인이 끝나면 여기로 돌려보낸다.
    web_base_url: str = "http://localhost:3000"

    # 세션. 값이 없으면 로그인 API 는 503 (NOT_CONFIGURED) 을 돌려준다.
    session_secret: SecretStr | None = None
    session_ttl_minutes: int = 60 * 24 * 7
    session_cookie_name: str = "anydeploy_session"
    # 로컬 http 개발에서는 false 로 둔다. 운영은 반드시 true.
    is_session_cookie_secure: bool = True

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
