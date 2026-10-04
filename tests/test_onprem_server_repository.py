"""OnpremServerRepository.save 가 유일 인덱스 위반을 가리는 방식. DB 없이 flush 실패를 흉내 낸다."""

import logging
from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import OnpremServerNameConflictError
from app.models.onprem_server import ONPREM_SERVER_NAME_INDEX, OnpremServer
from app.repositories.onprem_server_repository import OnpremServerRepository

_LOG_RECORD_ATTRIBUTES = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class _DriverError(Exception):
    """asyncpg 의 UniqueViolationError 처럼 위반한 제약 이름을 `constraint_name` 으로 준다."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.constraint_name = constraint_name


def _wrapped_in_orig(driver_error: Exception) -> Exception:
    # SQLAlchemy 2.1 asyncpg dialect: IntegrityError.orig.orig 가 드라이버 예외다.
    emulated = Exception("IntegrityError")
    emulated.orig = driver_error  # type: ignore[attr-defined]
    return emulated


def _wrapped_in_cause(driver_error: Exception) -> Exception:
    # SQLAlchemy 2.0 의 asyncpg dialect: 번역한 예외의 __cause__ 가 드라이버 예외다.
    translated = Exception("IntegrityError")
    translated.__cause__ = driver_error
    return translated


@pytest.fixture(params=[_wrapped_in_orig, _wrapped_in_cause], ids=["orig", "cause"])
def wrap(request: pytest.FixtureRequest) -> Callable[[Exception], Exception]:
    wrapper: Callable[[Exception], Exception] = request.param
    return wrapper


class _FailingSession:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        raise self._error


def _repository(session: _FailingSession) -> OnpremServerRepository:
    return OnpremServerRepository(session)  # type: ignore[arg-type]


def _integrity_error(orig: Exception) -> IntegrityError:
    return IntegrityError("INSERT INTO onprem_servers ...", {}, orig)


def _server() -> OnpremServer:
    return OnpremServer(owner_id=1, name="e2e-dup", server_key="k3x9q2ma")


async def test_save_name_index_violation_raises_name_conflict(
    wrap: Callable[[Exception], Exception],
) -> None:
    error = _integrity_error(wrap(_DriverError(ONPREM_SERVER_NAME_INDEX)))

    with pytest.raises(OnpremServerNameConflictError) as raised:
        await _repository(_FailingSession(error)).save(_server())

    assert raised.value.__cause__ is error
    # logging 의 extra 로 그대로 넘어가므로 LogRecord 속성 이름과 겹치면 응답이 500 이 된다.
    assert not _LOG_RECORD_ATTRIBUTES & set(raised.value.fields)


@pytest.mark.parametrize(
    "constraint_name",
    ["uq_onprem_servers_server_key", "uq_onprem_servers_target_id", "fk_onprem_servers_owner_id"],
)
async def test_save_other_constraint_violation_propagates_integrity_error(
    wrap: Callable[[Exception], Exception], constraint_name: str
) -> None:
    error = _integrity_error(wrap(_DriverError(constraint_name)))

    with pytest.raises(IntegrityError) as raised:
        await _repository(_FailingSession(error)).save(_server())

    assert raised.value is error


async def test_save_integrity_error_without_constraint_name_propagates() -> None:
    error = _integrity_error(_DriverError(None))

    with pytest.raises(IntegrityError) as raised:
        await _repository(_FailingSession(error)).save(_server())

    assert raised.value is error
