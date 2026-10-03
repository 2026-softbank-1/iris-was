"""FastAPI Depends 주입 함수. 객체 조립은 여기서만 한다."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.aws_clients import ArtifactStore, BuildLogReader, CloudWatchBuildLogClient
from app.clients.diagnosis_agent_client import HttpDiagnosisAgentClient
from app.clients.oauth_client import GithubOAuthClient
from app.clients.observability_client import LokiPrometheusObservabilityClient
from app.clients.repair_agent_client import HttpRepairAgentClient, HttpRepairSourceClient
from app.clients.source_repository_client import GithubSourceRepositoryClient
from app.core.config import Settings, get_settings
from app.core.crypto import VariableCipher
from app.core.database import get_session_factory
from app.core.exceptions import NotConfiguredError, UnauthorizedError
from app.models.user import User
from app.repositories.build_repository import BuildRepository
from app.repositories.cli_login_session_repository import CliLoginSessionRepository
from app.repositories.deployment_diagnosis_repository import DeploymentDiagnosisRepository
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import (
    DeploymentStatusHistoryRepository,
)
from app.repositories.github_installation_repository import GithubInstallationRepository
from app.repositories.job_repository import JobRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.release_repository import ReleaseRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_upload_repository import ServiceUploadRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.repositories.target_repository import TargetRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.automatic_repair_service import AutomaticRepairOpener, AutomaticRepairService
from app.services.cli_login_service import CliLoginService
from app.services.deployment_history_service import DeploymentHistoryService
from app.services.deployment_log_service import DeploymentLogService
from app.services.deployment_request_service import DeploymentRequestService
from app.services.deployment_status_service import DeploymentStatusService
from app.services.diagnosis_service import DiagnosisService, DiagnosisServiceOpener
from app.services.domain_service import DomainService
from app.services.manual_deployment_service import ManualDeploymentService
from app.services.observability_service import ObservabilityService
from app.services.project_service import ProjectService
from app.services.repair_github_auth_service import RepairGithubAuthService
from app.services.repair_handoff_service import RepairHandoffService
from app.services.repair_publication_service import RepairPublicationService
from app.services.repair_service import RepairService, RepairServiceOpener
from app.services.service_registry_service import ServiceRegistryService
from app.services.service_scaling_service import ServiceScalingService
from app.services.service_teardown_service import ServiceTeardownService
from app.services.session_service import SessionService
from app.services.source_repository_service import SourceRepositoryService
from app.services.target_service import TargetService
from app.services.upload_service import UploadService
from app.services.variable_service import VariableService
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


def get_cli_login_service(
    session: SessionDep, session_service: SessionServiceDep
) -> CliLoginService:
    return CliLoginService(
        session, CliLoginSessionRepository(session), UserRepository(session), session_service
    )


CliLoginServiceDep = Annotated[CliLoginService, Depends(get_cli_login_service)]


def get_auth_service(
    session: SessionDep,
    settings: SettingsDep,
    session_service: SessionServiceDep,
    cli_login_service: CliLoginServiceDep,
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
        cli_login_service,
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


def get_deployment_request_service(
    session: SessionDep, settings: SettingsDep
) -> DeploymentRequestService:
    return DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ServiceVariableRepository(session),
        ServiceRepository(session),
        deployment_strategy_enabled=settings.deployment_strategy_enabled,
    )


DeploymentRequestServiceDep = Annotated[
    DeploymentRequestService, Depends(get_deployment_request_service)
]


def get_service_teardown_service(
    session: SessionDep, deployment_request_service: DeploymentRequestServiceDep
) -> ServiceTeardownService:
    return ServiceTeardownService(DeploymentRequestRepository(session), deployment_request_service)


ServiceTeardownServiceDep = Annotated[ServiceTeardownService, Depends(get_service_teardown_service)]


def get_project_service(
    session: SessionDep, service_teardown_service: ServiceTeardownServiceDep
) -> ProjectService:
    return ProjectService(
        session, ProjectRepository(session), ServiceRepository(session), service_teardown_service
    )


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


def get_target_service(session: SessionDep) -> TargetService:
    return TargetService(TargetRepository(session))


TargetServiceDep = Annotated[TargetService, Depends(get_target_service)]


def get_service_registry_service(
    session: SessionDep,
    settings: SettingsDep,
    source_repository_service: SourceRepositoryServiceDep,
    service_teardown_service: ServiceTeardownServiceDep,
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
        service_teardown_service,
        deployment_strategy_enabled=settings.deployment_strategy_enabled,
    )


ServiceRegistryServiceDep = Annotated[ServiceRegistryService, Depends(get_service_registry_service)]


def get_domain_service(session: SessionDep) -> DomainService:
    return DomainService(
        ServiceRepository(session), TargetRepository(session), ReleaseRepository(session)
    )


DomainServiceDep = Annotated[DomainService, Depends(get_domain_service)]


def get_variable_service(session: SessionDep, settings: SettingsDep) -> VariableService:
    if settings.variables_encryption_key is None:
        raise NotConfiguredError(
            "variables encryption is not configured", setting="VARIABLES_ENCRYPTION_KEY"
        )
    return VariableService(
        session,
        ServiceRepository(session),
        ServiceVariableRepository(session),
        VariableCipher(settings.variables_encryption_key.get_secret_value()),
    )


VariableServiceDep = Annotated[VariableService, Depends(get_variable_service)]


def get_service_scaling_service(
    session: SessionDep, deployment_request_service: DeploymentRequestServiceDep
) -> ServiceScalingService:
    return ServiceScalingService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        deployment_request_service,
    )


ServiceScalingServiceDep = Annotated[ServiceScalingService, Depends(get_service_scaling_service)]


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
        BuildRepository(session),
        deployment_request_service,
        source_repository_service,
        ServiceUploadRepository(session),
    )


ManualDeploymentServiceDep = Annotated[
    ManualDeploymentService, Depends(get_manual_deployment_service)
]


def get_upload_service(session: SessionDep, settings: SettingsDep) -> UploadService:
    if not settings.aws_region or not settings.artifact_bucket:
        raise NotConfiguredError(
            "upload storage is not configured", setting="AWS_REGION, ARTIFACT_BUCKET"
        )
    return UploadService(
        session,
        ServiceRepository(session),
        ServiceUploadRepository(session),
        _get_artifact_store(settings.aws_region, settings.artifact_bucket),
        settings.upload_max_bytes,
    )


UploadServiceDep = Annotated[UploadService, Depends(get_upload_service)]


def get_deployment_history_service(session: SessionDep) -> DeploymentHistoryService:
    return DeploymentHistoryService(
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ReleaseRepository(session),
        TargetRepository(session),
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


def build_observability_service(
    session: AsyncSession, settings: Settings, http_client: httpx.AsyncClient
) -> ObservabilityService:
    return ObservabilityService(
        ServiceRepository(session),
        LokiPrometheusObservabilityClient(http_client),
        settings.loki_url,
        settings.prometheus_url,
        settings.traffic_cluster,
    )


def get_observability_service(
    session: SessionDep,
    settings: SettingsDep,
    http_client: HttpClientDep,
) -> ObservabilityService:
    return build_observability_service(session, settings, http_client)


ObservabilityServiceDep = Annotated[ObservabilityService, Depends(get_observability_service)]


@lru_cache
def _get_artifact_store(region: str, bucket: str) -> ArtifactStore:
    # boto3 클라이언트는 만드는 데 시간이 걸려 요청마다 만들지 않는다.
    return ArtifactStore(region, bucket)


def build_diagnosis_service(
    session: AsyncSession, settings: Settings, http_client: httpx.AsyncClient
) -> DiagnosisService:
    # 에이전트 설정이 없어도 저장된 진단은 조회할 수 있어야 하므로, 없다는 사실은 진단을 시작할 때
    # 서비스가 알린다.
    agent_client = (
        HttpDiagnosisAgentClient(
            http_client,
            str(settings.diagnosis_agent_url),
            settings.diagnosis_agent_api_key.get_secret_value(),
            settings.diagnosis_agent_timeout_seconds,
        )
        if settings.diagnosis_agent_url is not None and settings.diagnosis_agent_api_key is not None
        else None
    )
    # 스냅샷 버킷을 읽을 수 있을 때만 소스를 함께 보낸다. 없으면 로그만 진단한다.
    snapshot_client = (
        _get_artifact_store(settings.aws_region, settings.artifact_bucket)
        if settings.aws_region and settings.artifact_bucket
        else None
    )
    return DiagnosisService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        DeploymentDiagnosisRepository(session),
        build_observability_service(session, settings, http_client),
        agent_client,
        snapshot_client,
    )


def get_diagnosis_service(
    session: SessionDep, settings: SettingsDep, http_client: HttpClientDep
) -> DiagnosisService:
    return build_diagnosis_service(session, settings, http_client)


def build_diagnosis_service_opener(
    settings: Settings, http_client: httpx.AsyncClient
) -> DiagnosisServiceOpener:
    """요청이 끝난 뒤에도 진단을 이어 갈 수 있게, 새 DB 세션으로 서비스를 여는 함수를 만든다.

    요청 범위의 세션은 응답과 함께 닫히므로 백그라운드 작업이 그것을 쓰면 안 된다. 요청 없이
    도는 자동 진단도 이 함수로 서비스를 연다.
    """

    @asynccontextmanager
    async def open_service() -> AsyncIterator[DiagnosisService]:
        async with get_session_factory()() as session:
            yield build_diagnosis_service(session, settings, http_client)

    return open_service


def get_diagnosis_service_opener(
    settings: SettingsDep, http_client: HttpClientDep
) -> DiagnosisServiceOpener:
    return build_diagnosis_service_opener(settings, http_client)


DiagnosisServiceDep = Annotated[DiagnosisService, Depends(get_diagnosis_service)]
DiagnosisServiceOpenerDep = Annotated[DiagnosisServiceOpener, Depends(get_diagnosis_service_opener)]


@lru_cache
def _get_build_log_reader(region: str) -> CloudWatchBuildLogClient:
    # boto3 클라이언트는 만드는 데 시간이 걸려 요청마다 만들지 않는다.
    return CloudWatchBuildLogClient.create_reader(region)


def get_build_log_reader(settings: SettingsDep) -> BuildLogReader | None:
    """로그 전체를 읽으려면 AWS_REGION 과 BUILD_LOG_GROUP 이 모두 있어야 한다. 없으면 None 이다."""
    if settings.aws_region is None or settings.build_log_group is None:
        return None
    return _get_build_log_reader(settings.aws_region)


def get_deployment_log_service(
    session: SessionDep,
    settings: SettingsDep,
    deployment_history_service: DeploymentHistoryServiceDep,
    observability_service: ObservabilityServiceDep,
) -> DeploymentLogService:
    return DeploymentLogService(
        deployment_history_service,
        DeploymentRequestRepository(session),
        BuildRepository(session),
        observability_service,
        get_build_log_reader(settings),
        settings.build_log_group,
    )


DeploymentLogServiceDep = Annotated[DeploymentLogService, Depends(get_deployment_log_service)]


def build_repair_service(
    session: AsyncSession, settings: Settings, http_client: httpx.AsyncClient
) -> RepairService:
    hosts = tuple(
        host.strip().lower()
        for host in settings.repair_agent_source_hosts.split(",")
        if host.strip()
    )
    agent = (
        HttpRepairAgentClient(
            http_client,
            str(settings.repair_agent_url),
            settings.repair_agent_api_key.get_secret_value(),
            settings.repair_agent_timeout_seconds,
        )
        if settings.repair_agent_url is not None and settings.repair_agent_api_key is not None
        else None
    )
    handoff = (
        RepairHandoffService(agent, HttpRepairSourceClient(http_client, hosts))
        if agent is not None and hosts
        else None
    )
    snapshots = (
        _get_artifact_store(settings.aws_region, settings.artifact_bucket)
        if settings.aws_region and settings.artifact_bucket
        else None
    )
    return RepairService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        DeploymentDiagnosisRepository(session),
        DeploymentRepairRepository(session),
        agent,
        handoff,
        snapshots,
        max_cost_usd=settings.repair_agent_max_cost_usd,
        deadline_seconds=settings.repair_agent_deadline_seconds,
    )


def get_repair_service(
    session: SessionDep, settings: SettingsDep, http_client: HttpClientDep
) -> RepairService:
    return build_repair_service(session, settings, http_client)


def get_repair_service_opener(
    settings: SettingsDep, http_client: HttpClientDep
) -> RepairServiceOpener:
    @asynccontextmanager
    async def open_service() -> AsyncIterator[RepairService]:
        async with get_session_factory()() as session:
            yield build_repair_service(session, settings, http_client)

    return open_service


RepairServiceDep = Annotated[RepairService, Depends(get_repair_service)]
RepairServiceOpenerDep = Annotated[RepairServiceOpener, Depends(get_repair_service_opener)]


def get_repair_github_auth_service(
    session: SessionDep, repositories: SourceRepositoryServiceDep
) -> RepairGithubAuthService:
    return RepairGithubAuthService(ServiceRepository(session), repositories)


RepairGithubAuthServiceDep = Annotated[
    RepairGithubAuthService, Depends(get_repair_github_auth_service)
]


def get_repair_publication_service(
    session: SessionDep,
    settings: SettingsDep,
    candidates: RepairServiceDep,
    auth: RepairGithubAuthServiceDep,
) -> RepairPublicationService:
    return RepairPublicationService(
        session,
        DeploymentRepairRepository(session),
        ServiceRepository(session),
        candidates,
        auth,
        settings.github_api_base_url,
    )


RepairPublicationServiceDep = Annotated[
    RepairPublicationService, Depends(get_repair_publication_service)
]


def build_automatic_repair_service(
    session: AsyncSession, settings: Settings, http_client: httpx.AsyncClient
) -> AutomaticRepairService:
    credentials = get_github_app_credentials(settings)
    repositories = get_source_repository_service(session, settings, credentials, http_client)
    candidates = build_repair_service(session, settings, http_client)
    auth = RepairGithubAuthService(ServiceRepository(session), repositories)
    publication = RepairPublicationService(
        session,
        DeploymentRepairRepository(session),
        ServiceRepository(session),
        candidates,
        auth,
        settings.github_api_base_url,
    )
    return AutomaticRepairService(
        session,
        DeploymentRepairRepository(session),
        candidates,
        publication,
        build_diagnosis_service(session, settings, http_client),
        get_deployment_request_service(session, settings),
    )


def get_automatic_repair_service(
    session: SessionDep, settings: SettingsDep, http_client: HttpClientDep
) -> AutomaticRepairService:
    return build_automatic_repair_service(session, settings, http_client)


def build_automatic_repair_opener(
    settings: Settings, http_client: httpx.AsyncClient
) -> AutomaticRepairOpener:
    @asynccontextmanager
    async def open_service() -> AsyncIterator[AutomaticRepairService]:
        async with get_session_factory()() as session:
            yield build_automatic_repair_service(session, settings, http_client)

    return open_service


AutomaticRepairServiceDep = Annotated[AutomaticRepairService, Depends(get_automatic_repair_service)]
