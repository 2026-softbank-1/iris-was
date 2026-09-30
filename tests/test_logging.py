import json
import logging
import sys

from app.core.logging import ContextFilter, JsonFormatter, log_context


def _record(**extra: object) -> logging.LogRecord:
    return logging.makeLogRecord(
        {"name": "app.test", "levelname": "INFO", "levelno": logging.INFO, "msg": "build started"}
        | extra
    )


def _format(record: logging.LogRecord) -> dict[str, object]:
    ContextFilter("build-worker").filter(record)
    return json.loads(JsonFormatter().format(record))  # type: ignore[no-any-return]


def test_json_formatter_merges_component_context_extra_and_masks_secrets() -> None:
    with log_context(job_id=88, deployment_request_id=12):
        payload = _format(_record(codebuild_build_id="abc", api_token="s3cr3t"))

    assert payload["message"] == "build started"
    assert payload["component"] == "build-worker"
    assert payload["job_id"] == 88
    assert payload["deployment_request_id"] == 12
    assert payload["codebuild_build_id"] == "abc"
    assert payload["api_token"] == "***"


def test_log_context_extra_wins_and_resets_after_block() -> None:
    with log_context(job_id=1):
        inside = _format(_record(job_id=2))
    outside = _format(_record())

    assert inside["job_id"] == 2
    assert "job_id" not in outside


def test_json_formatter_exception_adds_type_and_stack() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("app.test").makeRecord(
            "app.test", logging.ERROR, __file__, 0, "failed", None, sys.exc_info()
        )

    payload = _format(record)

    assert payload["exc_type"] == "ValueError"
    assert "boom" in str(payload["stack"])
