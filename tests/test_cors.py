import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


async def _preflight(origin: str):  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.options(
            "/healthz",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "content-type,x-custom",
            },
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://likelion.uk",
        "https://www.likelion.uk",
        "https://a.b.likelion.uk",
        "http://localhost",
        "http://localhost:3000",
        "https://localhost:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
    ],
)
async def test_preflight_allowed_origin_echoes_origin_with_credentials(origin: str) -> None:
    response = await _preflight(origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "DELETE" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-headers"] == "content-type,x-custom"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.com",
        "https://likelion.uk.evil.com",
        "https://evillikelion.uk",
        "http://likelion.uk",
        "http://localhost.evil.com",
        "http://127.0.0.1.evil.com",
        "http://localhost:abc",
    ],
)
async def test_preflight_disallowed_origin_is_rejected(origin: str) -> None:
    response = await _preflight(origin)

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_simple_request_exposes_request_id_header() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/healthz", headers={"Origin": "http://localhost:3000"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]
