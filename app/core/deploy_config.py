from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeployWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str
    base_domain: str
    gitops_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    gitops_branch: str = "main"
    gitops_app_id: str
    gitops_app_private_key: SecretStr
    gitops_installation_id: int = Field(ge=1)
    github_api_base_url: str = "https://api.github.com"
    argocd_server_url: str
    argocd_token: SecretStr
    concurrency: int = Field(default=2, ge=1, le=16)
    lease_seconds: int = Field(default=300, ge=30)
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    reconcile_interval_seconds: float = Field(default=10.0, gt=0)
    # A platform ApplicationSet must use the same path/name templates.
    gitops_path_template: str = "services/{service_id}/{environment}"
    argo_application_template: str = "svc-{service_id}"


@lru_cache
def get_deploy_worker_settings() -> DeployWorkerSettings:
    return DeployWorkerSettings()
