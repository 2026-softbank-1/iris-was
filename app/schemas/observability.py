from dataclasses import asdict

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

    @classmethod
    def from_series(cls, series: MetricSeries) -> "MetricSeriesResponse":
        return cls(
            metric=series.metric,
            unit=series.unit,
            points=[MetricPointResponse(**asdict(point)) for point in series.points],
        )
