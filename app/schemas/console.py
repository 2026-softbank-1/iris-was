"""서비스 콘솔(Pod 셸) API·WebSocket 스키마. 계약은 docs/console-api.md, 결정은 ADR 0033."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.clients.kubernetes_client import PodInfo
from app.enums import ConsoleErrorCode, ConsoleUnavailableReason
from app.schemas.response import ApiModel

# input 프레임 하나의 최대 문자 수. 붙여넣기를 받되 프레임이 무한정 커지지 않게 한다.
MAX_INPUT_CHARS = 64 * 1024
MAX_TERMINAL_SIZE = 1000


# ---- Control API --------------------------------------------------------------------------


class ConsoleAvailabilityResponse(ApiModel):
    available: bool = Field(description="지금 콘솔을 열 수 있는지")
    reason: ConsoleUnavailableReason | None = Field(
        default=None,
        description=(
            "available=false 일 때의 사유. NO_RUNNING_DEPLOYMENT(떠 있는 배포 없음) · "
            "TARGET_NOT_SUPPORTED(온프레미스는 아직 지원하지 않음) · NOT_CONFIGURED(콘솔 설정 없음)"
        ),
    )


class CreateConsoleSessionRequest(ApiModel):
    target_id: int = Field(gt=0, description="콘솔을 열 타깃. 서비스에 연결된 타깃이어야 한다")


class ConsoleGatewayResponse(ApiModel):
    http_url: str = Field(
        description="Console Gateway REST base. `{httpUrl}/v1/pods` 로 Pod 목록을 읽는다",
        examples=["https://api.likelion.uk"],
    )
    ws_url: str = Field(
        description="Console Gateway WebSocket base. `{wsUrl}/v1/exec?pod={name}` 로 연결한다",
        examples=["wss://api.likelion.uk"],
    )


class ConsoleSessionResponse(ApiModel):
    session_id: str = Field(description="콘솔 세션 ID. ticket 의 jti 이자 감사 기록의 공개 ID")
    token: str = Field(
        description=(
            "Console Gateway 용 ticket(60초). Pod 목록은 `Authorization: Bearer`, 연결은 WebSocket "
            "첫 프레임 `auth` 로 보낸다. 연결에는 한 번만 쓸 수 있어 매번 새로 받는다"
        )
    )
    expires_at: datetime = Field(description="ticket 만료 시각(UTC)")
    gateway: ConsoleGatewayResponse


# ---- Console Gateway REST -----------------------------------------------------------------


class ConsolePodResponse(ApiModel):
    name: str = Field(examples=["app-6d9f7c-abcde"])
    phase: str = Field(description="Pod 단계. 삭제 중이면 Terminating", examples=["Running"])
    ready: bool = Field(description="app 컨테이너가 준비됐는지. 준비된 Pod 만 연결할 수 있다")
    started_at: datetime | None = None
    release_id: int | None = Field(
        default=None, description="Pod 라벨 `iris/release-id`. 알 수 없으면 없다"
    )

    @classmethod
    def from_info(cls, info: PodInfo) -> "ConsolePodResponse":
        return cls(
            name=info.name,
            phase=info.phase,
            ready=info.is_ready,
            started_at=info.started_at,
            release_id=info.release_id,
        )


class ConsolePodsResponse(ApiModel):
    pods: list[ConsolePodResponse] = Field(description="startedAt 내림차순")


# ---- Console Gateway WebSocket 프레임 (JSON 텍스트 프레임) ------------------------------------


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AuthFrame(_Frame):
    """연결 후 첫 프레임. 5초 안에 오지 않으면 Gateway 가 끊는다."""

    type: Literal["auth"] = "auth"
    token: str = Field(min_length=1)
    cols: int = Field(default=80, ge=1, le=MAX_TERMINAL_SIZE)
    rows: int = Field(default=24, ge=1, le=MAX_TERMINAL_SIZE)


class InputFrame(_Frame):
    type: Literal["input"] = "input"
    data: str = Field(max_length=MAX_INPUT_CHARS)


class ResizeFrame(_Frame):
    type: Literal["resize"] = "resize"
    cols: int = Field(ge=1, le=MAX_TERMINAL_SIZE)
    rows: int = Field(ge=1, le=MAX_TERMINAL_SIZE)


class PingFrame(_Frame):
    type: Literal["ping"] = "ping"


ConsoleClientFrame = Annotated[
    AuthFrame | InputFrame | ResizeFrame | PingFrame, Field(discriminator="type")
]
CONSOLE_CLIENT_FRAME_ADAPTER: TypeAdapter[ConsoleClientFrame] = TypeAdapter(ConsoleClientFrame)


class ReadyFrame(_Frame):
    type: Literal["ready"] = "ready"
    pod: str
    shell: Literal["bash", "sh"]


class OutputFrame(_Frame):
    """TTY 의 stdout+stderr(UTF-8, 깨진 바이트는 대체 문자)."""

    type: Literal["output"] = "output"
    data: str


class PongFrame(_Frame):
    type: Literal["pong"] = "pong"


class ExitFrame(_Frame):
    """셸이 끝났다. code 는 종료 코드이고, 알 수 없으면 없다."""

    type: Literal["exit"] = "exit"
    code: int | None = None


class ErrorFrame(_Frame):
    """오류. 이 프레임을 보낸 뒤 Gateway 가 소켓을 닫는다. 화면은 code 로 분기한다."""

    type: Literal["error"] = "error"
    code: ConsoleErrorCode
    message: str


ConsoleServerFrame = ReadyFrame | OutputFrame | PongFrame | ExitFrame | ErrorFrame
