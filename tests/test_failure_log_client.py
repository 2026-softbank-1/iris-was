"""Actual server-selected AWS sources, ordering, truncation and masking."""

import pytest

from app.clients.failure_log_client import FailureLogClient, _tail, mask_failure_log
from app.core.diagnosis_config import DiagnosisSettings
from app.core.exceptions import ExternalError


class CodeBuild:
    def batch_get_builds(self, **kwargs):
        assert kwargs == {"ids": ["iris-build:one"]}
        return {
            "builds": [
                {
                    "id": "iris-build:one",
                    "projectName": "iris-build",
                    "buildStatus": "FAILED",
                    "logs": {"groupName": "/aws/codebuild/iris-build", "streamName": "one"},
                }
            ]
        }


class CloudWatch:
    def __init__(self):
        self.calls = []

    def get_log_events(self, **kwargs):
        self.calls.append(kwargs)
        if "nextToken" not in kwargs:
            return {
                "events": [{"timestamp": 2, "message": "error: bare-secret"}],
                "nextBackwardToken": "older",
            }
        if kwargs["nextToken"] == "older":
            return {
                "events": [{"timestamp": 1, "message": "TOKEN=pattern-secret"}],
                "nextBackwardToken": "oldest",
            }
        return {"events": [], "nextBackwardToken": "oldest"}


async def test_cloudwatch_source_is_from_build_metadata_and_backward_pages_are_chronological():
    logs = CloudWatch()
    client = FailureLogClient(
        DiagnosisSettings(_env_file=None, codebuild_project="iris-build"),
        codebuild=CodeBuild(),
        cloudwatch=logs,
    )
    chunk, source, limitations = await client._collect_codebuild("iris-build:one", ["bare-secret"])
    assert chunk["text"] == "TOKEN=[REDACTED]\nerror: [REDACTED]"
    assert chunk["is_complete"]
    assert chunk["source_line_start"] is None
    assert source["log_stream"] == "one"
    assert len(logs.calls) == 3
    assert not limitations
    assert all(item["logGroupName"] == "/aws/codebuild/iris-build" for item in logs.calls)


async def test_wrong_build_identity_is_rejected_without_log_fetch():
    class WrongBuild:
        def batch_get_builds(self, **kwargs):
            return {"builds": [{"id": "other-build"}]}

    logs = CloudWatch()
    client = FailureLogClient(
        DiagnosisSettings(_env_file=None), codebuild=WrongBuild(), cloudwatch=logs
    )
    with pytest.raises(ExternalError):
        await client._collect_codebuild("iris-build:one", [])
    assert not logs.calls


def test_known_values_pattern_secrets_and_multiline_keys_are_redacted():
    original = (
        "plain bare-secret\nDATABASE_URL=postgres://user:pass@host/db\nTOKEN=abcdef\n"
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    )
    masked = mask_failure_log(original, ["bare-secret"])
    assert "bare-secret" not in masked
    assert "user:pass" not in masked
    assert "abcdef" not in masked
    assert "secret\n" not in masked
    assert masked.count("\n") == original.count("\n")
    tail, omitted = _tail("old line\n" + "new line\n" * 100, 100)
    assert omitted and len(tail.encode()) <= 100
