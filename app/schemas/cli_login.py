from pydantic import Field

from app.enums import CliLoginSessionStatus
from app.schemas.response import ApiModel
from app.services.cli_login_service import CliLoginPoll, CliLoginStart


class CliLoginSessionResponse(ApiModel):
    session_id: str = Field(
        description="추측할 수 없는 공개 ID. verificationUrl 에 들어간다",
        examples=["Zk3v1u0eQ8mY2nXr5oPq7sT9wUa4bC6dEfGhIjKlMnO"],
    )
    poll_secret: str = Field(
        description="CLI 만 아는 비밀. 폴링할 때만 쓰고, 이 응답 말고는 다시 볼 수 없다",
        examples=["Xy9Qw2Er4Ty6Ui8Op0As1Df3Gh5Jk7Lz9Cv1Bn3Mq5W"],
    )
    verification_url: str = Field(
        description="브라우저로 열 주소. GitHub 로그인으로 이어진다",
        examples=["https://api.likelion.uk/api/v1/auth/cli/sessions/<sessionId>/authorize"],
    )
    expires_in: int = Field(description="세션 유효 시간(초)", examples=[600])
    interval: int = Field(
        description="폴링 간격(초). 이보다 빠르면 429 가 될 수 있다", examples=[2]
    )

    @classmethod
    def from_start(cls, start: CliLoginStart, verification_url: str) -> "CliLoginSessionResponse":
        return cls(
            session_id=start.session_id,
            poll_secret=start.poll_secret,
            verification_url=verification_url,
            expires_in=start.expires_in,
            interval=start.interval,
        )


class PollCliLoginTokenRequest(ApiModel):
    poll_secret: str = Field(
        min_length=1, max_length=256, description="세션을 만들 때 받은 pollSecret"
    )


class CliLoginTokenResponse(ApiModel):
    status: CliLoginSessionStatus = Field(
        description=(
            "PENDING 승인 전 · APPROVED 승인됨(처음 한 번만 accessToken 을 준다) · "
            "DENIED 거절됨 · EXPIRED 만료됐거나 토큰을 이미 가져감"
        )
    )
    access_token: str | None = Field(
        default=None,
        description="세션 JWT. `Authorization: Bearer` 로 보낸다. APPROVED 로 처음 답할 때만 있다",
    )

    @classmethod
    def from_poll(cls, poll: CliLoginPoll) -> "CliLoginTokenResponse":
        return cls(status=poll.status, access_token=poll.access_token)
