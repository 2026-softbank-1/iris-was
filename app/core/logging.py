"""구조화 로깅 — stdout 에 JSON 한 줄씩 출력한다.

필드는 다섯 갈래로 붙는다.
- 기본: timestamp·level·logger·message — 포매터가 LogRecord 에서 채운다.
- 프로세스: component — configure_logging 인자로 고정한다.
- 컨텍스트: log_context(...) 블록 안의 모든 로그에 붙는다 (request_id, job_id 등).
- 이벤트: logger.info("고정 문구", extra={...}) 로 넘긴 필드. 예외 fields 처럼 바깥에서 온 키를
  펼칠 때는 build_extra 로 감싼다(예약 속성과 겹치면 logging 이 KeyError 를 낸다).
- 예외: exc_info 가 있으면 exc_type·stack.
"""

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType

_HANDLER_NAME = "json_stdout"
# Logger.makeRecord 가 extra 키로 거부하는 이름이다. LogRecord 기본 속성(name·args·module·taskName
# 등)에 포매터가 채우는 message·asctime 을 더한다. 파이썬 버전마다 달라서 레코드에서 구한다.
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
# color_message: uvicorn 이 터미널 색상용으로 넣는 ANSI 코드 필드다.
_STANDARD_ATTRS = _RESERVED_RECORD_ATTRS | {"color_message"}
# build_extra 가 겹친 키를 보존할 때 앞에 붙이는 접두사.
_FIELD_PREFIX = "field_"
_SENSITIVE_KEYWORDS = ("password", "secret", "token", "authorization")
_NOISY_LOGGERS = ("httpx", "httpcore", "botocore", "sqlalchemy.engine")

_EMPTY_CONTEXT: Mapping[str, object] = MappingProxyType({})
_log_context: ContextVar[Mapping[str, object]] = ContextVar("log_context", default=_EMPTY_CONTEXT)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """블록 안에서 찍히는 모든 로그에 fields 를 붙인다. 블록을 벗어나면 이전 값으로 돌아간다."""
    token = _log_context.set({**_log_context.get(), **fields})
    try:
        yield
    finally:
        _log_context.reset(token)


def build_extra(fixed: Mapping[str, object], fields: Mapping[str, object]) -> dict[str, object]:
    """logger 의 extra 를 만든다.

    fixed 는 호출부가 정한 키, fields 는 예외 fields 처럼 바깥에서 온 키다. fields 의 키가 LogRecord
    예약 속성(name·message·args 등)이나 fixed 의 키(action·error_code 등)와 겹치면 `field_` 접두사를
    붙여 값을 보존한다. 예약 속성을 그대로 넘기면 logging 이 KeyError 를 내고, fixed 를 덮어쓰면
    로그의 action 을 믿을 수 없다. 겹치지 않는 키는 그대로 둔다.
    """
    extra = dict(fixed)
    clashing: dict[str, object] = {}
    for key, value in fields.items():
        if key in extra or key in _STANDARD_ATTRS:
            clashing[key] = value
        else:
            extra[key] = value
    for key, value in clashing.items():
        renamed = _FIELD_PREFIX + key
        # 접두사를 붙인 이름도 이미 쓰고 있으면 한 번 더 붙여 어떤 값도 버리지 않는다.
        while renamed in extra or renamed in _STANDARD_ATTRS:
            renamed = _FIELD_PREFIX + renamed
        extra[renamed] = value
    return extra


class ContextFilter(logging.Filter):
    """component 와 현재 log_context 를 레코드에 붙인다. 같은 키면 extra 가 우선한다."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component

    def filter(self, record: logging.LogRecord) -> bool:
        record.component = self._component
        for key, value in _log_context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = "***" if _is_sensitive(key) else value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(keyword in lowered for keyword in _SENSITIVE_KEYWORDS)


def configure_logging(component: str, level: str = "INFO") -> None:
    """프로세스 진입점에서 1회 호출한다. 다시 호출해도 핸들러를 중복으로 달지 않는다."""
    root = logging.getLogger()
    if any(handler.get_name() == _HANDLER_NAME for handler in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    # 핸들러에 달아야 하위 로거에서 전파된 레코드에도 적용된다.
    handler.addFilter(ContextFilter(component))
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 로그도 같은 JSON 으로 내보낸다. 접근 로그는 RequestContextMiddleware 가 남긴다.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
