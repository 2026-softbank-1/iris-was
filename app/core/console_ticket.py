"""서비스 콘솔 ticket 의 서명·검증(EdDSA/Ed25519 JWT).

Control API 가 개인키로 서명하고 Console Gateway 가 공개키로 검증한다. 서명키를 둘이 공유하지
않는다(ADR 0033). ticket 은 60초짜리이고, 만료·1회용 검사는 Gateway 가 한다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.exceptions import ConsoleTokenExpiredError, NotConfiguredError, UnauthorizedError

CONSOLE_TICKET_ISSUER = "iris-control-api"
CONSOLE_TICKET_AUDIENCE = "iris-console-gateway"
CONSOLE_TICKET_TTL = timedelta(seconds=60)
# 서명 시각과 만료 시각의 간격 상한. 이보다 길게 서명된 ticket 은 받지 않는다.
MAX_TICKET_LIFETIME_SECONDS = int(CONSOLE_TICKET_TTL.total_seconds())
# 서버 사이 시계 오차 허용(초).
CLOCK_LEEWAY_SECONDS = 5
# Gateway 가 Pod 에 닿는 경로의 식별자. aws 는 Prod EKS API 로 직접, onprem 은 Argo CD 터미널로
# 닿는다(ADR 0035). Control API 가 타깃 종류에서 정하고 요청에서 받지 않는다.
CONSOLE_CLUSTER_AWS = "aws"
CONSOLE_CLUSTER_ONPREM = "onprem"

_ALGORITHM = "EdDSA"


@dataclass(frozen=True)
class ConsoleTicketClaims:
    """검증을 마친 ticket. session_id 는 `console_sessions.public_id` 이자 jti 다."""

    session_id: str
    user_id: int
    service_id: int
    target_id: int
    namespace: str
    cluster: str
    issued_at: datetime
    expires_at: datetime


def build_service_namespace(service_id: int) -> str:
    """서비스가 배포되는 Kubernetes namespace. 로그·메트릭·콘솔이 같은 규칙을 쓴다."""
    return f"svc-{service_id}"


def _normalize_pem(pem: str) -> bytes:
    # 환경변수에는 줄바꿈을 `\n` 두 글자로 적어도 된다.
    return pem.replace("\\n", "\n").strip().encode()


class ConsoleTicketSigner:
    """Control API 용. 개인키로 ticket 을 서명한다."""

    def __init__(self, private_key_pem: str) -> None:
        try:
            key = serialization.load_pem_private_key(_normalize_pem(private_key_pem), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise NotConfiguredError(
                "console ticket private key is invalid", setting="CONSOLE_TICKET_PRIVATE_KEY"
            ) from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise NotConfiguredError(
                "console ticket private key must be Ed25519", setting="CONSOLE_TICKET_PRIVATE_KEY"
            )
        self._key = key

    def sign(
        self,
        session_id: str,
        user_id: int,
        service_id: int,
        target_id: int,
        cluster: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """(ticket, 만료 시각)을 만든다. namespace 는 service_id 에서 정한다."""
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + CONSOLE_TICKET_TTL
        payload: dict[str, Any] = {
            "iss": CONSOLE_TICKET_ISSUER,
            "aud": CONSOLE_TICKET_AUDIENCE,
            "sub": str(user_id),
            "jti": session_id,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "svc": service_id,
            "tid": target_id,
            "ns": build_service_namespace(service_id),
            "cluster": cluster,
        }
        return jwt.encode(payload, self._key, algorithm=_ALGORITHM), expires_at


class ConsoleTicketVerifier:
    """Console Gateway 용. 공개키로 ticket 을 검증한다."""

    def __init__(self, public_key_pem: str) -> None:
        try:
            key = serialization.load_pem_public_key(_normalize_pem(public_key_pem))
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise NotConfiguredError(
                "console ticket public key is invalid", setting="CONSOLE_TICKET_PUBLIC_KEY"
            ) from exc
        if not isinstance(key, Ed25519PublicKey):
            raise NotConfiguredError(
                "console ticket public key must be Ed25519", setting="CONSOLE_TICKET_PUBLIC_KEY"
            )
        self._key = key

    def verify(self, token: str) -> ConsoleTicketClaims:
        """서명·발급자·대상·만료와 namespace 가 service_id 에서 나온 값인지 검사한다."""
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[_ALGORITHM],
                audience=CONSOLE_TICKET_AUDIENCE,
                issuer=CONSOLE_TICKET_ISSUER,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "jti", "sub", "svc", "tid", "ns", "cluster"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ConsoleTokenExpiredError("console ticket expired") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("invalid console ticket") from exc
        try:
            parsed = ConsoleTicketClaims(
                session_id=str(claims["jti"]),
                user_id=int(claims["sub"]),
                service_id=int(claims["svc"]),
                target_id=int(claims["tid"]),
                namespace=str(claims["ns"]),
                cluster=str(claims["cluster"]),
                issued_at=datetime.fromtimestamp(int(claims["iat"]), UTC),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
            )
        except (TypeError, ValueError) as exc:
            raise UnauthorizedError("invalid console ticket") from exc
        lifetime = (parsed.expires_at - parsed.issued_at).total_seconds()
        if lifetime > MAX_TICKET_LIFETIME_SECONDS:
            raise UnauthorizedError("console ticket lifetime too long")
        # 요청이 아니라 ticket 의 service_id 가 namespace 를 정한다. 어긋나면 위조·버그다.
        if parsed.namespace != build_service_namespace(parsed.service_id):
            raise UnauthorizedError("console ticket namespace mismatch")
        return parsed
