from dataclasses import asdict
from datetime import datetime
from typing import Self

from pydantic import Field

from app.clients.aws_clients import LogLine
from app.clients.observability_client import NetworkLogEntry
from app.enums import BuildStatus
from app.schemas.observability import LogEntryResponse
from app.schemas.response import ApiModel


class BuildLogEntryResponse(ApiModel):
    timestamp_ns: str = Field(
        description="로그 시각(Unix ns). JS 정밀도 손실을 피하려고 문자열이다.",
        examples=["1790812800000000000"],
    )
    message: str

    @classmethod
    def from_line(cls, line: LogLine) -> Self:
        return cls(timestamp_ns=str(line.timestamp_ms * 1_000_000), message=line.message)


class BuildLogsResponse(ApiModel):
    entries: list[BuildLogEntryResponse] = Field(description="시간 오름차순")
    next_cursor: str | None = Field(
        default=None,
        description=(
            "다음 호출의 cursor. 진행 중인 빌드는 이 값으로 이어서 호출해 새 로그를 받는다. "
            "읽을 로그 스트림이 없으면 보낸 cursor 가 그대로 돌아온다."
        ),
    )
    build_status: BuildStatus | None = Field(
        default=None, description="로그를 만든 빌드의 상태. 빌드가 없으면 없다."
    )
    is_complete: bool = Field(
        description="빌드가 끝났고 이번 호출에서 읽은 로그가 없다. true 이면 폴링을 멈춘다."
    )
    is_partial: bool = Field(
        default=False,
        description=(
            "CloudWatch 를 읽을 수 없는 환경에서 Build Worker 가 남긴 실패한 빌드의 끝부분만 "
            "돌려줬고 앞부분이 빠졌다. 이때 nextCursor 는 없다."
        ),
    )
    logged_deployment_id: int | None = Field(
        default=None,
        description=(
            "로그를 만든 빌드가 속한 배포 id. 롤백·재시작은 빌드를 새로 하지 않아 원본 배포다. "
            "읽을 로그가 없으면 없다."
        ),
    )


class DeploymentLogsResponse(ApiModel):
    entries: list[LogEntryResponse] = Field(description="시간 오름차순")
    is_truncated: bool = Field(description="limit 에 도달했다. 범위를 좁혀 더 조회한다.")
    start: datetime | None = Field(
        default=None, description="실제로 조회한 구간 시작. 조회할 것이 없으면 없다."
    )
    end: datetime | None = Field(
        default=None, description="실제로 조회한 구간 끝. 조회할 것이 없으면 없다."
    )


class NetworkLogEntryResponse(ApiModel):
    timestamp_ns: str = Field(examples=["1790812800000000000"])
    status: int = Field(description="사용자에게 돌려준 ALB 최종 응답 코드", examples=[200])
    target_status: int | None = Field(
        default=None, description="서비스(Pod)가 돌려준 응답 코드. ALB 가 직접 응답하면 없다."
    )
    received_bytes: int = Field(description="ALB 가 받은 요청 바이트")
    sent_bytes: int = Field(description="ALB 가 보낸 응답 바이트")
    response_time_seconds: float | None = Field(
        default=None, description="ALB 가 서비스에 요청을 보내고 응답 헤더를 받기까지(초)"
    )

    @classmethod
    def from_entry(cls, entry: NetworkLogEntry) -> Self:
        return cls(**asdict(entry))


class NetworkLogsResponse(ApiModel):
    entries: list[NetworkLogEntryResponse] = Field(description="시간 오름차순")
    is_truncated: bool = Field(description="limit 에 도달했다. 범위를 좁혀 더 조회한다.")
    start: datetime | None = Field(
        default=None, description="실제로 조회한 구간 시작. 서비스한 적 없는 배포는 없다."
    )
    end: datetime | None = Field(
        default=None, description="실제로 조회한 구간 끝. 서비스한 적 없는 배포는 없다."
    )
