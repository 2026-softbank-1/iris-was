"""Console Gateway 객체 조립. 설정에서 클러스터 Client 와 서비스를 만든다."""

import ssl

import httpx

from app.clients.aws_clients import EksTokenProvider
from app.clients.kubernetes_client import HttpKubernetesClient
from app.core.config import ConsoleGatewaySettings
from app.core.console_ticket import CONSOLE_CLUSTER_AWS, ConsoleTicketVerifier
from app.services.console_gateway_service import (
    ConsoleGatewayService,
    ConsoleLimits,
    ConsoleSessionRegistry,
)


def build_console_gateway_service(
    settings: ConsoleGatewaySettings, http_client: httpx.AsyncClient, ssl_context: ssl.SSLContext
) -> ConsoleGatewayService:
    token_provider = EksTokenProvider(settings.console_aws_cluster_name, settings.aws_region)
    aws_cluster = HttpKubernetesClient(
        http_client, str(settings.console_aws_cluster_endpoint), ssl_context, token_provider
    )
    return ConsoleGatewayService(
        ConsoleTicketVerifier(settings.console_ticket_public_key),
        {CONSOLE_CLUSTER_AWS: aws_cluster},
        ConsoleSessionRegistry(settings.console_max_sessions_per_user),
        ConsoleLimits(
            idle_timeout_seconds=settings.console_idle_timeout_seconds,
            max_session_seconds=settings.console_max_session_seconds,
            max_sessions_per_user=settings.console_max_sessions_per_user,
        ),
    )
