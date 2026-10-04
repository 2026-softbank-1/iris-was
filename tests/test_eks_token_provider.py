import base64
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.exceptions import NoCredentialsError

from app.clients.aws_clients import EKS_TOKEN_CACHE_SECONDS, EKS_TOKEN_PREFIX, EksTokenProvider
from app.core.exceptions import ClusterUnavailableError

REGION = "ap-northeast-2"
CLUSTER = "iris-dev-workload"


def _client_factory(*args: Any, **kwargs: Any) -> Any:
    """고정 자격증명으로 만든 STS 클라이언트. 네트워크 호출 없이 서명만 한다."""
    return boto3.client(
        *args,
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="example-secret",
        **kwargs,
    )


def _decode(token: str) -> str:
    assert token.startswith(EKS_TOKEN_PREFIX)
    encoded = token.removeprefix(EKS_TOKEN_PREFIX)
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


async def test_get_token_signs_cluster_id_header_into_presigned_url() -> None:
    provider = EksTokenProvider(CLUSTER, REGION, _client_factory)

    url = _decode(await provider.get_token())

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.hostname == f"sts.{REGION}.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert "x-k8s-aws-id" in query["X-Amz-SignedHeaders"][0].split(";")
    # ClusterName 은 STS 파라미터가 아니라 서명할 헤더다.
    assert "ClusterName" not in query


async def test_get_token_has_no_base64_padding() -> None:
    token = await EksTokenProvider(CLUSTER, REGION, _client_factory).get_token()

    assert "=" not in token


async def test_get_token_is_cached_until_expiry() -> None:
    now = [1000.0]
    presign_calls = []

    def counting_factory(*args: Any, **kwargs: Any) -> Any:
        client = _client_factory(*args, **kwargs)
        original = client.generate_presigned_url

        def counted(*a: Any, **kw: Any) -> str:
            presign_calls.append(1)
            return original(*a, **kw)

        client.generate_presigned_url = counted
        return client

    provider = EksTokenProvider(CLUSTER, REGION, counting_factory, lambda: now[0])

    first = await provider.get_token()
    now[0] += EKS_TOKEN_CACHE_SECONDS - 1
    assert await provider.get_token() == first
    assert len(presign_calls) == 1

    now[0] += 2
    await provider.get_token()

    assert len(presign_calls) == 2


async def test_get_token_without_credentials_raises_cluster_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_credentials_factory(*args: Any, **kwargs: Any) -> Any:
        client = boto3.client(*args, **kwargs)

        def fail(*_: Any, **__: Any) -> str:
            raise NoCredentialsError

        client.generate_presigned_url = fail
        return client

    provider = EksTokenProvider(CLUSTER, REGION, no_credentials_factory)

    with pytest.raises(ClusterUnavailableError):
        await provider.get_token()


async def test_get_token_uses_regional_sts_endpoint() -> None:
    created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    factory: Callable[..., Any] = _client_factory

    def recording_factory(*args: Any, **kwargs: Any) -> Any:
        created.append((args, kwargs))
        return factory(*args, **kwargs)

    EksTokenProvider(CLUSTER, REGION, recording_factory)

    ((args, kwargs),) = created
    assert args == ("sts",)
    assert kwargs["endpoint_url"] == f"https://sts.{REGION}.amazonaws.com"
    assert kwargs["region_name"] == REGION
