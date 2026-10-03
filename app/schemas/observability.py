from dataclasses import asdict

from pydantic import Field

from app.clients.observability_client import LogEntry, MetricSeries, TrafficMetrics
from app.schemas.response import ApiModel


class LogEntryResponse(ApiModel):
    timestamp_ns: str
    message: str
    pod: str
    container: str

    @classmethod
    def from_entry(cls, entry: LogEntry) -> "LogEntryResponse":
        return cls(**asdict(entry))


class LogsResponse(ApiModel):
    entries: list[LogEntryResponse]
    is_truncated: bool


class MetricPointResponse(ApiModel):
    timestamp: float
    value: float


class MetricSeriesResponse(ApiModel):
    metric: str
    unit: str
    points: list[MetricPointResponse]
    pod: str | None = Field(
        default=None,
        description="Pod(replica) 이름. groupBy=pod 일 때만 있고 total 이면 생략된다.",
        examples=["app-5d9c7b8f6d-x2k4q"],
    )

    @classmethod
    def from_series(cls, series: MetricSeries) -> "MetricSeriesResponse":
        return cls(
            metric=series.metric,
            unit=series.unit,
            points=[MetricPointResponse(**asdict(point)) for point in series.points],
            pod=series.pod,
        )


class TrafficMetricsResponse(ApiModel):
    available_until: float = Field(
        description=(
            "Unix 초. 이 시각까지의 이벤트만 집계가 끝났다. 이후 구간은 집계 대기(약 15분 지연)라 "
            "빈 것이 아니라 아직 모르는 값이다."
        ),
        examples=[1790952000.0],
    )
    series: list[MetricSeriesResponse]

    @classmethod
    def from_traffic(cls, traffic: TrafficMetrics) -> "TrafficMetricsResponse":
        return cls(
            available_until=traffic.available_until,
            series=[MetricSeriesResponse.from_series(item) for item in traffic.series],
        )
