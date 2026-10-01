"""FastAPI Depends 주입 함수. 객체 조립은 여기서만 한다."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

import httpx
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.oauth_client import GithubOAuthClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.exceptions import NotConfiguredError, UnauthorizedError
from app.models.user import User
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.job_repository import JobRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.target_repository import TargetRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.deployment_history_service import DeploymentHistoryService
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.project_service import ProjectService
from app.services.service_registry_service import ServiceRegistryService
from app.services.session_service import SessionService
from app.services.source_repository_service import SourceRepositoryService
from app.services.target_service import TargetService
from app.services.webhook_service import WebhookService

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def _require_session_secret(settings: Settings) -> str:
    if settings.session_secret is None:
        raise NotConfiguredError("session is not configured", setting="SESSION_SECRET")
    return settings.session_secret.get_secret_value()


def get_session_service(session: SessionDep, settings: SettingsDep) -> SessionService:
    return SessionService(
        UserRepository(session),
        _require_session_secret(settings),
        timedelta(minutes=settings.session_ttl_minutes),
    )


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]


@dataclass(frozen=True)
class GithubLoginCredentials:
    client_id: str
    client_secret: str


def get_github_login_credentials(settings: SettingsDep) -> GithubLoginCredentials:
    if settings.github_app_client_id is None or settings.github_app_client_secret is None:
        raise NotConfiguredError(
            "github login is not configured",
            setting="GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET",
        )
    return GithubLoginCredentials(
        settings.github_app_client_id, settings.github_app_client_secret.get_secret_value()
    )


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    session_service: SessionServiceDep,
    credentials: Annotated[GithubLoginCredentials, Depends(get_github_login_credentials)],
    http_client: HttpClientDep,
) -> AuthService:
    oauth_client = GithubOAuthClient(
        http_client,
        credentials.client_id,
        credentials.client_secret,
        settings.github_web_base_url,
        settings.github_api_base_url,
    )
    return AuthService(
        session,
        UserRepository(session),
        GithubInstallationRepository(session),
        oauth_client,
        session_service,
        _require_session_secret(settings),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


@dataclass(frozen=True)
class GithubAppCredentials:
    app_id: str
    private_key: str


def get_github_app_credentials(settings: SettingsDep) -> GithubAppCredentials:
    if settings.github_app_id is None or settings.github_app_private_key is None:
        raise NotConfiguredError(
            "github app is not configured", setting="GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY"
        )
    return GithubAppCredentials(
        settings.github_app_id, settings.github_app_private_key.get_secret_value()
    )


def get_source_repository_service(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[GithubAppCredentials, Depends(get_github_app_credentials)],
    http_client: HttpClientDep,
) -> SourceRepositoryService:
    client = GithubSourceRepositoryClient(
        http_client, credentials.app_id, credentials.private_key, settings.github_api_base_url
    )
    return SourceRepositoryService(GithubInstallationRepository(session), client)


SourceRepositoryServiceDep = Annotated[
    SourceRepositoryService, Depends(get_source_repository_service)
]


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(session, ProjectRepository(session), ServiceRepository(session))


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


def get_target_service(session: SessionDep) -> TargetService:
    return TargetService(TargetRepository(session))


TargetServiceDep = Annotated[TargetService, Depends(get_target_service)]


def get_service_registry_service(
    session: SessionDep, source_repository_service: SourceRepositoryServiceDep
) -> ServiceRegistryService:
    # 저장소 연결·브랜치 확인에 GitHub App 설정이 필요하므로 이 서비스를 쓰는 API 는 모두 요구한다.
    return ServiceRegistryService(
        session,
        ProjectRepository(session),
        ServiceRepository(session),
        TargetRepository(session),
        GithubInstallationRepository(session),
        source_repository_service,
        DeploymentRequestRepository(session),
    )


ServiceRegistryServiceDep = Annotated[ServiceRegistryService, Depends(get_service_registry_service)]


def get_deployment_request_service(session: SessionDep) -> DeploymentRequestService:
    return DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
    )


DeploymentRequestServiceDep = Annotated[
    DeploymentRequestService, Depends(get_deployment_request_service)
]


def get_deployment_status_service(session: SessionDep) -> DeploymentStatusService:
    return DeploymentStatusService(
        DeploymentRequestRepository(session), DeploymentStatusHistoryRepository(session)
    )


DeploymentStatusServiceDep = Annotated[
    DeploymentStatusService, Depends(get_deployment_status_service)
]


def get_manual_deployment_service(
    session: SessionDep,
    deployment_request_service: DeploymentRequestServiceDep,
    source_repository_service: SourceRepositoryServiceDep,
) -> ManualDeploymentService:
    # 브랜치 최신 커밋을 GitHub 에서 읽으므로 GitHub App 설정이 필요하다.
    return ManualDeploymentService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        deployment_request_service,
        source_repository_service,
    )


ManualDeploymentServiceDep = Annotated[
    ManualDeploymentService, Depends(get_manual_deployment_service)
]


def get_deployment_history_service(session: SessionDep) -> DeploymentHistoryService:
    return DeploymentHistoryService(
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        DeploymentStatusHistoryRepository(session),
    )


DeploymentHistoryServiceDep = Annotated[
    DeploymentHistoryService, Depends(get_deployment_history_service)
]


def get_webhook_service(
    session: SessionDep,
    settings: SettingsDep,
    deployment_request_service: DeploymentRequestServiceDep,
) -> WebhookService:
    if settings.github_webhook_secret is None:
        raise NotConfiguredError(
            "github webhook is not configured", setting="GITHUB_WEBHOOK_SECRET"
        )
    return WebhookService(
        session,
        ServiceRepository(session),
        GithubInstallationRepository(session),
        deployment_request_service,
        settings.github_webhook_secret.get_secret_value(),
    )


WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]


# 문서(Swagger)에 인증 방식을 알리는 선언이다. 쿠키는 웹, Bearer 는 CLI 가 쓴다.
_cookie_scheme = APIKeyCookie(name=get_settings().session_cookie_name, auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False, description="CLI 용 세션 토큰")


async def get_current_user(
    session_service: SessionServiceDep,
    cookie_token: Annotated[str | None, Security(_cookie_scheme)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
) -> User:
    """쿠키(웹) 또는 `Authorization: Bearer`(CLI)의 세션 토큰으로 현재 사용자를 찾는다."""
    token = bearer.credentials if bearer is not None else cookie_token
    if not token:
        raise UnauthorizedError("login required")
    return await session_service.get_user(token)


CurrentUserDep = Annotated[User, Depends(get_current_user)]
