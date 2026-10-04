import asyncio
import logging
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import (
    BuildFailedError,
    ConflictError,
    NotFoundError,
    RepositoryAnalysisFailedError,
)
from app.core.middleware import RequestContextMiddleware
from app.enums import AnalysisErrorCode, FailureCode
from app.schemas.response import ApiModel
from app.services.analysis_gate_service import AnalysisGateService
from app.workers import build_worker


class CreateDeploymentRequest(ApiModel):
    source_sha: str


def _create_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.post("/deployments")
    async def create_deployment(request: CreateDeploymentRequest) -> None:
        raise ConflictError("deployment in progress", service_id=3)

    @app.get("/deployments/{deployment_id}")
    async def get_deployment(deployment_id: int) -> None:
        raise NotFoundError(deployment_id=deployment_id)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    return app


async def _request(method: str, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=_create_app()), base_url="http://t") as c:
        return await c.request(method, url, **kwargs)  # type: ignore[arg-type]


async def test_app_error_returns_envelope_and_request_id_header() -> None:
    response = await _request("POST", "/deployments", json={"sourceSha": "abc"})

    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "code": "CONFLICT",
        "message": "deployment in progress",
    }
    assert len(response.headers["X-Request-ID"]) == 32


async def test_validation_error_returns_camel_case_details() -> None:
    response = await _request("POST", "/deployments", json={})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["details"] == [{"field": "sourceSha", "reason": "Field required"}]


async def test_unhandled_exception_returns_internal_error_envelope() -> None:
    response = await _request("GET", "/boom")

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "code": "INTERNAL_ERROR",
        "message": "internal server error",
    }


async def test_unknown_route_returns_not_found_envelope() -> None:
    response = await _request("GET", "/nope")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_access_log_uses_route_template(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.middleware"):
        await _request("GET", "/deployments/7")

    access = next(r for r in caplog.records if r.getMessage() == "request completed")
    assert access.route == "/deployments/{deployment_id}"
    assert access.status_code == 404


# --- 예외 fields 가 logging 예약 속성과 겹쳐도 로깅 때문에 500 이 되면 안 된다 ---

# logging 이 extra 키로 거부하는 이름. 코드와 같은 방식으로 구한다.
RESERVED_KEYS = sorted(frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"})


class ReservedFieldsConflictError(ConflictError):
    """name·message·args·action 같은 fields 를 가진 예외. 생성자 인자로는 message 를 fields 로 못
    넘기므로 fields 를 직접 채운다."""

    code = "RESERVED_FIELDS_CONFLICT"

    def __init__(self, fields: dict[str, object]) -> None:
        super().__init__("conflict with reserved log fields")
        self.fields = fields


class ReservedFieldsExternalError(ReservedFieldsConflictError):
    code = "RESERVED_FIELDS_EXTERNAL"
    status_code = 502


def _reserved_fields_app(error: ReservedFieldsConflictError) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/reserved")
    async def reserved() -> None:
        raise error

    return app


async def _get_reserved(error: ReservedFieldsConflictError) -> Any:
    transport = ASGITransport(app=_reserved_fields_app(error))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get("/reserved")


@pytest.mark.parametrize("error_type", [ReservedFieldsConflictError, ReservedFieldsExternalError])
async def test_app_error_with_log_record_field_names_keeps_status_code_and_body(
    error_type: type[ReservedFieldsConflictError], caplog: pytest.LogCaptureFixture
) -> None:
    fields: dict[str, object] = {key: f"value-{key}" for key in RESERVED_KEYS}
    fields["service_id"] = 3
    error = error_type(fields)

    with caplog.at_level(logging.INFO, logger="app.core"):
        response = await _get_reserved(error)

    assert response.status_code == error.status_code
    assert response.json() == {
        "success": False,
        "code": error.code,
        "message": "conflict with reserved log fields",
    }
    record = next(r for r in caplog.records if r.name == "app.core.exception_handlers")
    assert record.levelno == (logging.ERROR if error.status_code >= 500 else logging.INFO)
    assert record.getMessage() == (
        "request failed" if error.status_code >= 500 else "request rejected"
    )
    assert record.action == "handle_app_error"  # type: ignore[attr-defined]
    assert record.error_code == error.code  # type: ignore[attr-defined]
    assert record.service_id == 3  # type: ignore[attr-defined]
    for key in RESERVED_KEYS:
        assert record.__dict__[f"field_{key}"] == f"value-{key}"


@pytest.mark.parametrize("key", ["name", "message", "args", "action"])
async def test_app_error_with_single_reserved_field_responds_normally(
    key: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core"):
        response = await _get_reserved(ReservedFieldsConflictError({key: "edge-1"}))

    assert response.status_code == 409
    assert response.json()["code"] == "RESERVED_FIELDS_CONFLICT"
    assert not any(r.getMessage() == "unhandled exception" for r in caplog.records)


async def test_app_error_fields_cannot_override_handler_log_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields: dict[str, object] = {"action": "evil", "error_code": "evil"}

    with caplog.at_level(logging.INFO, logger="app.core"):
        await _get_reserved(ReservedFieldsConflictError(fields))

    record = next(r for r in caplog.records if r.name == "app.core.exception_handlers")
    assert record.action == "handle_app_error"  # type: ignore[attr-defined]
    assert record.error_code == "RESERVED_FIELDS_CONFLICT"  # type: ignore[attr-defined]
    assert record.field_action == "evil"  # type: ignore[attr-defined]
    assert record.field_error_code == "evil"  # type: ignore[attr-defined]


async def test_app_error_log_shape_is_unchanged_when_fields_do_not_overlap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core"):
        await _request("POST", "/deployments", json={"sourceSha": "abc"})

    record = next(r for r in caplog.records if r.name == "app.core.exception_handlers")
    assert record.getMessage() == "request rejected"
    assert record.action == "handle_app_error"  # type: ignore[attr-defined]
    assert record.error_code == "CONFLICT"  # type: ignore[attr-defined]
    assert record.service_id == 3  # type: ignore[attr-defined]
    assert not [key for key in record.__dict__ if key.startswith("field_")]


async def test_build_worker_logs_build_failed_fields_that_collide_with_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed: list[BuildFailedError] = []

    class Service:
        async def run(self, job: object, stop: asyncio.Event) -> None:
            raise BuildFailedError(FailureCode.SOURCE_INVALID, "bad source", name="web", args=1)

        def fail(self, job: object, exc: BuildFailedError) -> Coroutine[Any, Any, None]:
            async def record() -> None:
                failed.append(exc)

            return record()

    job = SimpleNamespace(id=1, kind="BUILD", deployment_request_id=2)

    with caplog.at_level(logging.INFO, logger="app.workers"):
        await build_worker._process(Service(), job, asyncio.Event())  # type: ignore[arg-type]

    assert len(failed) == 1  # 로깅이 터져 실패 기록이 건너뛰어지면 안 된다.
    record = next(r for r in caplog.records if r.getMessage() == "build failed")
    assert record.failure_code == FailureCode.SOURCE_INVALID  # type: ignore[attr-defined]
    assert record.field_name == "web"  # type: ignore[attr-defined]
    assert record.field_args == 1  # type: ignore[attr-defined]


async def test_analysis_gate_logs_failure_fields_that_collide_with_log_record(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[RepositoryAnalysisFailedError] = []
    service = AnalysisGateService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(analysis_gate_timeout_seconds=60),  # type: ignore[arg-type]
        "worker-1",
    )

    async def analyze(analysis: object) -> None:
        raise RepositoryAnalysisFailedError(
            AnalysisErrorCode.ANALYZER_FAILED, "analyzer failed", name="shop", module="gate"
        )

    async def record_failure(analysis_id: int, error: RepositoryAnalysisFailedError) -> None:
        recorded.append(error)

    monkeypatch.setattr(service, "_analyze", analyze)
    monkeypatch.setattr(service, "_record_failure", record_failure)

    with caplog.at_level(logging.INFO, logger="app.services"):
        await service.run(SimpleNamespace(id=7, attempts=1))  # type: ignore[arg-type]

    assert len(recorded) == 1  # 로깅이 터져 실패 기록이 건너뛰어지면 안 된다.
    record = next(r for r in caplog.records if r.getMessage() == "repository analysis failed")
    assert record.error_code == AnalysisErrorCode.ANALYZER_FAILED  # type: ignore[attr-defined]
    assert record.field_name == "shop"  # type: ignore[attr-defined]
    assert record.field_module == "gate"  # type: ignore[attr-defined]
