import json
import logging
import sys

import pytest

from app.core.logging import ContextFilter, JsonFormatter, build_extra, log_context

# logging 이 extra 키로 거부하는 이름(Logger.makeRecord 의 검사 기준)을 코드와 같은 방식으로 구한다.
RESERVED_KEYS = sorted(frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"})


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


def test_build_extra_keeps_fixed_and_non_overlapping_keys_unchanged() -> None:
    extra = build_extra(
        {"action": "handle_app_error", "error_code": "CONFLICT"},
        {"service_id": 3, "onprem_server_name": "edge-1"},
    )

    assert extra == {
        "action": "handle_app_error",
        "error_code": "CONFLICT",
        "service_id": 3,
        "onprem_server_name": "edge-1",
    }


def test_build_extra_renames_log_record_attributes_with_prefix_and_keeps_values() -> None:
    extra = build_extra({"action": "x"}, {"name": "edge-1", "message": "m", "args": [1]})

    assert extra == {"action": "x", "field_name": "edge-1", "field_message": "m", "field_args": [1]}


def test_build_extra_does_not_let_fields_override_fixed_keys() -> None:
    extra = build_extra(
        {"action": "handle_app_error", "error_code": "CONFLICT"},
        {"action": "evil", "error_code": "evil"},
    )

    assert extra == {
        "action": "handle_app_error",
        "error_code": "CONFLICT",
        "field_action": "evil",
        "field_error_code": "evil",
    }


def test_build_extra_never_drops_a_field_when_the_prefixed_name_is_taken() -> None:
    extra = build_extra({"action": "x"}, {"name": 1, "field_name": 2})

    assert sorted(extra.values(), key=str) == sorted(["x", 1, 2], key=str)
    assert extra["field_name"] == 2
    assert not set(extra) & set(RESERVED_KEYS)


def test_build_extra_does_not_mutate_its_inputs() -> None:
    fixed = {"action": "x"}
    fields = {"name": "edge-1"}

    build_extra(fixed, fields)

    assert fixed == {"action": "x"}
    assert fields == {"name": "edge-1"}


@pytest.mark.parametrize("key", RESERVED_KEYS)
def test_logging_rejects_raw_reserved_key_but_accepts_build_extra(
    key: str, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("app.test.reserved")

    with caplog.at_level(logging.INFO, logger="app.test.reserved"):
        # 막으려는 버그: 예약 속성을 extra 키로 그대로 넘기면 logging 이 KeyError 를 낸다.
        with pytest.raises(KeyError):
            logger.info("rejected", extra={key: "value"})

        logger.info("accepted", extra=build_extra({"action": "test"}, {key: "value"}))

    record = caplog.records[-1]
    assert record.getMessage() == "accepted"
    assert record.__dict__[f"field_{key}"] == "value"
    assert record.action == "test"  # type: ignore[attr-defined]


def test_renamed_fields_reach_the_json_output_without_touching_standard_keys() -> None:
    record = logging.getLogger("app.test").makeRecord(
        "app.test",
        logging.INFO,
        __file__,
        0,
        "request rejected",
        (),
        None,
        extra=build_extra({"action": "handle_app_error"}, {"name": "edge-1", "service_id": 3}),
    )

    payload = _format(record)

    assert payload["logger"] == "app.test"
    assert payload["message"] == "request rejected"
    assert payload["field_name"] == "edge-1"
    assert payload["service_id"] == 3
    assert "name" not in payload
