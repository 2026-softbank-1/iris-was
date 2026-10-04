import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.console_gateway.main import create_app
from app.core.config import ConsoleGatewaySettings, Settings, get_console_gateway_settings
from app.dependencies import get_console_service
from app.services.console_gateway_service import ConsoleGatewayService
from app.services.console_service import ConsoleGatewayAddress
from tests.fakes import FakeSession
from tests.fakes_console import generate_ed25519_pem_pair


def _ca_base64() -> str:
    """테스트용 자체 서명 CA 인증서(PEM)를 EKS 가 주는 형식(base64)으로 만든다."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode()


def _gateway_env(**overrides: str) -> dict[str, str]:
    _, public_pem = generate_ed25519_pem_pair()
    env = {
        "CONSOLE_TICKET_PUBLIC_KEY": public_pem,
        "CONSOLE_AWS_CLUSTER_NAME": "iris-dev-workload",
        "CONSOLE_AWS_CLUSTER_ENDPOINT": "https://ABC123.gr7.ap-northeast-2.eks.amazonaws.com",
        "CONSOLE_AWS_CLUSTER_CA": _ca_base64(),
        "AWS_REGION": "ap-northeast-2",
        "CONSOLE_ALLOWED_ORIGINS": "https://app.likelion.uk, https://app.dev.likelion.uk",
    }
    env.update(overrides)
    return env


@pytest.fixture
def gateway_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = _gateway_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_console_gateway_settings.cache_clear()
    yield env  # type: ignore[misc]
    get_console_gateway_settings.cache_clear()


# ---- Console Gateway 설정 --------------------------------------------------------------------


def test_gateway_settings_parse_env_and_defaults(gateway_env: dict[str, str]) -> None:
    settings = ConsoleGatewaySettings()  # type: ignore[call-arg]

    assert settings.allowed_origins == ["https://app.likelion.uk", "https://app.dev.likelion.uk"]
    assert settings.console_idle_timeout_seconds == 900
    assert settings.console_max_session_seconds == 3600
    assert settings.console_max_sessions_per_user == 3
    assert settings.console_aws_cluster_name == "iris-dev-workload"


def test_gateway_settings_do_not_require_database_url(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert ConsoleGatewaySettings(_env_file=None).console_aws_cluster_name  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "missing",
    [
        "CONSOLE_TICKET_PUBLIC_KEY",
        "CONSOLE_AWS_CLUSTER_NAME",
        "CONSOLE_AWS_CLUSTER_ENDPOINT",
        "CONSOLE_AWS_CLUSTER_CA",
        "AWS_REGION",
        "CONSOLE_ALLOWED_ORIGINS",
    ],
)
def test_gateway_settings_require_each_contract_variable(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError):
        ConsoleGatewaySettings(_env_file=None)  # type: ignore[call-arg]


def test_gateway_settings_reject_non_https_cluster_endpoint(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSOLE_AWS_CLUSTER_ENDPOINT", "http://cluster.example")

    with pytest.raises(ValidationError):
        ConsoleGatewaySettings(_env_file=None)  # type: ignore[call-arg]


def test_gateway_settings_validation_error_does_not_leak_secret_values(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSOLE_AWS_CLUSTER_ENDPOINT", "http://cluster.example")

    with pytest.raises(ValidationError) as exc_info:
        ConsoleGatewaySettings(_env_file=None)  # type: ignore[call-arg]

    assert gateway_env["CONSOLE_TICKET_PUBLIC_KEY"] not in str(exc_info.value)


async def test_gateway_lifespan_builds_service_from_settings(gateway_env: dict[str, str]) -> None:
    app = create_app()

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.console_gateway_service, ConsoleGatewayService)
        assert app.state.console_allowed_origins == frozenset(
            {"https://app.likelion.uk", "https://app.dev.likelion.uk"}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            assert (await http.get("/readyz")).status_code == 204


async def test_gateway_lifespan_fails_fast_on_invalid_ca(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSOLE_AWS_CLUSTER_CA", "%%%")
    get_console_gateway_settings.cache_clear()
    app = create_app()

    with pytest.raises(Exception, match="cluster ca is invalid"):
        async with app.router.lifespan_context(app):
            pass


async def test_gateway_lifespan_fails_fast_on_non_ed25519_public_key(
    gateway_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSOLE_TICKET_PUBLIC_KEY", "garbage")
    get_console_gateway_settings.cache_clear()
    app = create_app()

    with pytest.raises(Exception, match="public key is invalid"):
        async with app.router.lifespan_context(app):
            pass


# ---- Control API 설정 ------------------------------------------------------------------------


def _control_settings(**values: str) -> Settings:
    return Settings(
        _env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db", **values
    )  # type: ignore[arg-type]


def test_control_settings_console_is_off_by_default() -> None:
    settings = _control_settings()

    assert settings.console_ticket_private_key is None
    assert settings.console_gateway_http_url is None
    assert settings.console_gateway_ws_url is None


def test_get_console_service_builds_gateway_address_without_trailing_slash() -> None:
    private_pem, _ = generate_ed25519_pem_pair()
    settings = _control_settings(
        console_ticket_private_key=private_pem,
        console_gateway_http_url="https://api.likelion.uk/console/",
        console_gateway_ws_url="wss://api.likelion.uk",
    )

    service = get_console_service(FakeSession(), settings)  # type: ignore[arg-type]

    # 응답에 그대로 나가는 값이라 끝의 `/` 를 뺀 base 여야 화면이 `{base}/v1/…` 로 붙일 수 있다.
    assert service._gateway == ConsoleGatewayAddress(
        "https://api.likelion.uk/console", "wss://api.likelion.uk"
    )


def test_get_console_service_is_unconfigured_when_any_setting_is_missing() -> None:
    private_pem, _ = generate_ed25519_pem_pair()
    settings = _control_settings(
        console_ticket_private_key=private_pem,
        console_gateway_http_url="https://api.likelion.uk/console",
    )

    service = get_console_service(FakeSession(), settings)  # type: ignore[arg-type]

    assert service._gateway is None


def test_control_settings_reject_non_websocket_gateway_url() -> None:
    with pytest.raises(ValidationError):
        _control_settings(console_gateway_ws_url="https://api.likelion.uk/console")
