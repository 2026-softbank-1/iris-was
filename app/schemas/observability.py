from dataclasses import asdict

from pydantic import Field

from app.clients.observability_client import LogEntry, MetricSeries
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
