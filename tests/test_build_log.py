from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.clients.aws_clients import (
    BuildLogTail,
    CloudWatchBuildLogClient,
    CodeBuildClient,
    LogLine,
)
from app.core.exceptions import ExternalError
from app.services.build_log import (
    MAX_ENTRIES,
    MAX_MESSAGE_CHARS,
    MAX_TOTAL_BYTES,
    redact_log_message,
    to_log_tail,
)

T0_MS = 1_790_000_000_000  # 2026-09-21T14:13:20Z


def _tail(*messages: str, is_truncated: bool = False) -> BuildLogTail:
    return BuildLogTail(
        lines=[LogLine(T0_MS + i * 1000, message) for i, message in enumerate(messages)],
        is_truncated=is_truncated,
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "curl https://b.s3.amazonaws.com/snapshots/1.tar.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIAEXAMPLE%2F20261003&X-Amz-Signature=abcdef0123 -o src.tgz",
            "curl https://b.s3.amazonaws.com/snapshots/1.tar.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=[REDACTED]&X-Amz-Signature=[REDACTED] -o src.tgz",
        ),
        ("Authorization: Bearer abc.def.ghi", "Authorization: Bearer [REDACTED]"),
        ("authorization: basic dXNlcjpwYXNz", "authorization: basic [REDACTED]"),
        ("token=ghs_" + "A" * 36 + " end", "token=[REDACTED] end"),
        ("pat github_pat_" + "B" * 30, "pat [REDACTED]"),
        ("npm ERR! missing script: build", "npm ERR! missing script: build"),
    ],
)
def test_redact_log_message_masks_known_secret_shapes_only(message: str, expected: str) -> None:
    assert redact_log_message(message) == expected


def test_to_log_tail_keeps_order_formats_iso_timestamps_and_strips_newlines() -> None:
    result = to_log_tail(_tail("first\n", "second\r\n"))

    assert result == {
        "entries": [
            {"timestamp": "2026-09-21T14:13:20Z", "message": "first"},
            {"timestamp": "2026-09-21T14:13:21Z", "message": "second"},
        ],
        "is_truncated": False,
    }


def test_to_log_tail_skips_blank_lines_and_redacts_before_storing() -> None:
    result = to_log_tail(_tail("   ", "\n", "Authorization: Bearer secret-token", "boom"))

    assert [e["message"] for e in result["entries"]] == [
        "Authorization: Bearer [REDACTED]",
        "boom",
    ]


def test_to_log_tail_keeps_only_newest_entries_over_the_limit_and_marks_truncated() -> None:
    result = to_log_tail(_tail(*[f"line {i}" for i in range(MAX_ENTRIES + 25)]))

    assert len(result["entries"]) == MAX_ENTRIES
    assert result["entries"][-1]["message"] == f"line {MAX_ENTRIES + 24}"
    assert result["entries"][0]["message"] == "line 25"
    assert result["is_truncated"] is True


def test_to_log_tail_limits_total_bytes_and_each_message() -> None:
    long_lines = ["x" * (MAX_MESSAGE_CHARS + 500) for _ in range(60)]

    result = to_log_tail(_tail(*long_lines))

    assert all(len(e["message"]) == MAX_MESSAGE_CHARS for e in result["entries"])
    assert sum(len(e["message"].encode()) for e in result["entries"]) <= MAX_TOTAL_BYTES
    assert result["is_truncated"] is True


def test_to_log_tail_preserves_truncated_flag_from_source() -> None:
    assert to_log_tail(_tail("only", is_truncated=True))["is_truncated"] is True


class _StubLogs:
    def __init__(self, events: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.events = events or []
        self.error = error
        self.kwargs: dict[str, Any] = {}

    def get_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return {"events": self.events}


async def test_cloudwatch_client_reads_latest_lines_in_order() -> None:
    stub = _StubLogs([{"timestamp": 5, "message": "a\n"}, {"timestamp": 9, "message": "b\n"}])

    tail = await CloudWatchBuildLogClient("ap-northeast-2", stub).fetch_tail("group", "stream", 10)

    assert stub.kwargs == {
        "logGroupName": "group",
        "logStreamName": "stream",
        "limit": 10,
        "startFromHead": False,
    }
    assert [(line.timestamp_ms, line.message) for line in tail.lines] == [(5, "a\n"), (9, "b\n")]
    assert tail.is_truncated is False


async def test_cloudwatch_client_marks_truncated_when_page_is_full() -> None:
    stub = _StubLogs([{"timestamp": i, "message": str(i)} for i in range(3)])

    tail = await CloudWatchBuildLogClient("ap-northeast-2", stub).fetch_tail("g", "s", 3)

    assert tail.is_truncated is True


@pytest.mark.parametrize("code", ["AccessDeniedException", "ResourceNotFoundException"])
async def test_cloudwatch_client_turns_aws_errors_into_external_error(code: str) -> None:
    error = ClientError({"Error": {"Code": code, "Message": "arn:aws:logs:secret"}}, "GetLogEvents")

    with pytest.raises(ExternalError) as raised:
        await CloudWatchBuildLogClient("ap-northeast-2", _StubLogs(error=error)).fetch_tail(
            "g", "s", 5
        )

    assert "secret" not in str(raised.value)


async def test_codebuild_get_build_exposes_log_group_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubCodeBuild:
        def batch_get_builds(self, ids: list[str]) -> dict[str, Any]:
            return {
                "builds": [
                    {
                        "buildStatus": "FAILED",
                        "phases": [{"phaseType": "BUILD", "phaseStatus": "FAILED"}],
                        "logs": {
                            "deepLink": "https://console/logs",
                            "groupName": "/aws/codebuild/iris-dev-build",
                            "streamName": "9d1e",
                        },
                    }
                ]
            }

    client = CodeBuildClient("ap-northeast-2", "iris-dev-build")
    monkeypatch.setattr(client, "_client", StubCodeBuild())

    result = await client.get_build("iris-dev-build:9d1e")

    assert (result.status, result.failed_phase) == ("FAILED", "BUILD")
    assert result.log_group == "/aws/codebuild/iris-dev-build"
    assert result.log_stream == "9d1e"
    assert result.log_url == "https://console/logs"
