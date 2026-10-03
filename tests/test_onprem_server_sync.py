"""서버 동기화의 순수 부분(values·cluster config 렌더링)과 ECR pull 자격증명 Client."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.clients.aws_clients import EcrPullCredentialClient
from app.clients.secret_sealer import SecretSealer
from app.core.exceptions import NotConfiguredError
from app.enums import Builder
from app.services.builder_detection import DeployConfig
from app.services.deploy_service import ONPREM_ECR_PULL_SECRET, render_service_values
from app.services.onprem_server_sync_service import (
    build_cluster_config,
    cluster_secret_name,
    render_server_values,
)
from tests.sealed_support import make_controller_key, unseal

KEY = "k3x9q2ma"
FQDN = f"iris-{KEY}.tailb046e8.ts.net"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_build_cluster_config_has_token_ca_and_server_name() -> None:
    config = json.loads(build_cluster_config("sa-token", CA, FQDN))

    assert config == {
        "bearerToken": "sa-token",
        "tlsClientConfig": {
            "caData": base64.b64encode(CA.encode()).decode(),
            "serverName": FQDN,
        },
    }


def test_render_server_values_matches_contract_shape() -> None:
    values = json.loads(
        render_server_values(server_key=KEY, tailnet_fqdn=FQDN, ca_pem=CA, encrypted_config="AgB")
    )

    assert values == {
        "server": {
            "key": KEY,
            "clusterName": f"onprem-{KEY}",
            "tailnetFqdn": FQDN,
            "apiPort": 6443,
            "appsPort": 80,
        },
        "cluster": {"caData": base64.b64encode(CA.encode()).decode(), "encryptedConfig": "AgB"},
    }


async def test_cluster_config_is_sealed_for_argocd_cluster_secret_only() -> None:
    controller_key, certificate = make_controller_key()
    config = build_cluster_config("sa-token", CA, FQDN)

    sealed = await SecretSealer(certificate).seal(
        "argocd", cluster_secret_name(KEY), {"config": config}
    )

    assert "sa-token" not in sealed["config"]
    assert unseal(controller_key, sealed["config"], "argocd", f"cluster-onprem-{KEY}") == config
    with pytest.raises(ValueError):
        unseal(controller_key, sealed["config"], "argocd", "cluster-onprem-other000")


class FakeAws:
    """boto3.client 대역. sts·ecr 호출 인자를 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # 연쇄된 role 세션은 1시간, ECR 토큰은 12시간이다.
        self.credentials_expire = datetime.now(UTC) + timedelta(hours=1)
        self.token_expires = datetime.now(UTC) + timedelta(hours=12)

    def __call__(self, service: str, **kwargs: Any) -> "FakeAws":
        self.calls.append((f"client:{service}", kwargs))
        return self

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("assume_role", kwargs))
        return {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": self.credentials_expire,
            }
        }

    def get_authorization_token(self) -> dict[str, Any]:
        token = base64.b64encode(b"AWS:ecr-password").decode()
        return {
            "authorizationData": [{"authorizationToken": token, "expiresAt": self.token_expires}]
        }


async def test_issue_pull_credential_scopes_session_policy_to_repositories() -> None:
    aws = FakeAws()
    client = EcrPullCredentialClient(
        "ap-northeast-2", "arn:aws:iam::123456789012:role/iris-dev-onprem-ecr-pull", 3600, aws
    )

    credential = await client.issue_pull_credential(
        "iris-onprem-k3x9q2ma", ["iris/services/12", "iris/services/15"]
    )

    assert credential.registry == "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
    assert (credential.username, credential.password) == ("AWS", "ecr-password")
    assert credential.expires_at == aws.credentials_expire
    assume = next(kwargs for name, kwargs in aws.calls if name == "assume_role")
    assert assume["RoleSessionName"] == "iris-onprem-k3x9q2ma"
    assert assume["DurationSeconds"] == 3600
    statements = json.loads(assume["Policy"])["Statement"]
    assert statements[0] == {
        "Effect": "Allow",
        "Action": "ecr:GetAuthorizationToken",
        "Resource": "*",
    }
    assert statements[1]["Resource"] == [
        "arn:aws:ecr:ap-northeast-2:123456789012:repository/iris/services/12",
        "arn:aws:ecr:ap-northeast-2:123456789012:repository/iris/services/15",
    ]
    assert "ecr:BatchGetImage" in statements[1]["Action"]
    ecr_client = next(kwargs for name, kwargs in aws.calls if name == "client:ecr")
    assert ecr_client["aws_session_token"] == "session"


async def test_issue_pull_credential_expiry_is_token_when_it_ends_first() -> None:
    aws = FakeAws()
    aws.token_expires = aws.credentials_expire - timedelta(minutes=10)
    client = EcrPullCredentialClient(
        "ap-northeast-2", "arn:aws:iam::123456789012:role/iris-dev-onprem-ecr-pull", 3600, aws
    )

    credential = await client.issue_pull_credential("iris-onprem-k3x9q2ma", ["iris/services/12"])

    assert credential.expires_at == aws.token_expires


def test_pull_credential_client_rejects_invalid_role_arn() -> None:
    with pytest.raises(NotConfiguredError):
        EcrPullCredentialClient("ap-northeast-2", "not-an-arn", 3600, FakeAws())


def test_render_service_values_writes_image_pull_secrets_for_server_target() -> None:
    values = json.loads(
        render_service_values(
            host_label=f"api-12-{KEY}",
            release_id=1,
            image_repository="123.dkr.ecr.ap-northeast-2.amazonaws.com/iris/services/12",
            image_digest="sha256:abc",
            source_sha="f" * 40,
            builder=Builder.RAILPACK,
            deploy=DeployConfig(),
            base_domain="internal.likelion.uk",
            image_pull_secrets=[ONPREM_ECR_PULL_SECRET],
        )
    )

    assert values["imagePullSecrets"] == [{"name": "iris-ecr-pull"}]
    assert values["route"]["host"] == f"api-12-{KEY}.internal.likelion.uk"
