import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import ConflictError, NotFoundError
from app.core.middleware import RequestContextMiddleware
from app.schemas.response import ApiModel


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
