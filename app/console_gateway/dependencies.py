"""Console Gateway 객체 조립. 설정에서 클러스터 Client 와 서비스를 만든다."""

import ssl
from contextlib import AsyncExitStack

import httpx

from app.clients.argocd_terminal_client import ArgoCdTerminalClient
from app.clients.aws_clients import EksTokenProvider
from app.clients.kubernetes_client import (
    HttpKubernetesClient,
    KubernetesClient,
    build_cluster_ssl_context,
)
from app.core.config import ConsoleGatewaySettings
from app.core.console_ticket import (
    CONSOLE_CLUSTER_AWS,
    CONSOLE_CLUSTER_ONPREM,
    ConsoleTicketVerifier,
)
from app.services.console_gateway_service import (
    ConsoleGatewayService,
    ConsoleLimits,
    ConsoleSessionRegistry,
)

# 클러스터 REST 호출 제한 시간(초). exec WebSocket 의 연결 제한은 Client 가 따로 둔다.
CLUSTER_HTTP_TIMEOUT_SECONDS = 10.0


async def build_console_gateway_service(
    settings: ConsoleGatewaySettings, stack: AsyncExitStack
) -> ConsoleGatewayService:
    """설정된 클러스터마다 Client 를 만든다. HTTP 클라이언트는 stack 이 닫는다."""
    clusters: dict[str, KubernetesClient] = {}
    if (
        settings.console_aws_cluster_name is not None
        and settings.console_aws_cluster_endpoint is not None
        and settings.console_aws_cluster_ca is not None
        and settings.aws_region is not None
    ):
        ssl_context = build_cluster_ssl_context(settings.console_aws_cluster_ca)
        aws_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=str(settings.console_aws_cluster_endpoint),
                verify=ssl_context,
                timeout=CLUSTER_HTTP_TIMEOUT_SECONDS,
            )
        )
        clusters[CONSOLE_CLUSTER_AWS] = HttpKubernetesClient(
            aws_http,
            str(settings.console_aws_cluster_endpoint),
            ssl_context,
            EksTokenProvider(settings.console_aws_cluster_name, settings.aws_region),
        )
    if settings.console_argocd_server_url is not None and settings.console_argocd_token is not None:
        # Argo CD 서버 인증서는 클러스터 내부 CA 로 서명돼 있다. 시스템 CA 에 더한 번들은
        # SSL_CERT_FILE(chart 가 마운트)로 받는다.
        argocd_ssl_context = ssl.create_default_context()
        argocd_http = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=str(settings.console_argocd_server_url),
                verify=argocd_ssl_context,
                timeout=CLUSTER_HTTP_TIMEOUT_SECONDS,
            )
        )
        clusters[CONSOLE_CLUSTER_ONPREM] = ArgoCdTerminalClient(
            argocd_http,
            str(settings.console_argocd_server_url),
            settings.console_argocd_token,
            argocd_ssl_context,
            project=settings.console_argocd_project,
            app_namespace=settings.console_argocd_app_namespace,
        )
    return ConsoleGatewayService(
        ConsoleTicketVerifier(settings.console_ticket_public_key),
        clusters,
        ConsoleSessionRegistry(settings.console_max_sessions_per_user),
        ConsoleLimits(
            idle_timeout_seconds=settings.console_idle_timeout_seconds,
            max_session_seconds=settings.console_max_session_seconds,
            max_sessions_per_user=settings.console_max_sessions_per_user,
        ),
    )
