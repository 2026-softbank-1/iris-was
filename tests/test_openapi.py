"""API 문서 규칙을 지키는지 검사한다. 엔드포인트를 추가하면 이 테스트가 빠진 문서화를 알려 준다."""

import json
from typing import Any

import pytest

from app.main import app
from scripts.export_openapi import OPENAPI_PATH, render_openapi

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
# 인증·입력이 없는 운영용 경로는 문서 규칙에서 제외한다.
OPERATIONAL_PATHS = {"/healthz", "/readyz"}


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    spec = app.openapi()
    return [
        (method.upper(), path, operation)
        for path, item in spec["paths"].items()
        if path not in OPERATIONAL_PATHS
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


@pytest.mark.parametrize(
    ("method", "path", "operation"), _operations(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_operation_is_documented(method: str, path: str, operation: dict[str, Any]) -> None:
    where = f"{method} {path}"
    responses = operation["responses"]

    assert operation.get("summary") and operation["summary"] != operation["operationId"], (
        f"{where}: summary 가 필요하다"
    )
    assert operation.get("tags"), f"{where}: tags 가 필요하다"
    if operation.get("security"):
        assert "401" in responses, f"{where}: 인증이 필요한 API 는 401 응답을 문서화한다"
    if operation.get("parameters") or operation.get("requestBody"):
        assert "422" in responses, f"{where}: 입력이 있는 API 는 422 응답을 문서화한다"
    assert "HTTPValidationError" not in json.dumps(responses), (
        f"{where}: 422 는 error_responses(422) 로 ApiResponse 봉투를 문서화한다"
    )


def test_exported_openapi_file_is_up_to_date() -> None:
    command = "uv run python -m scripts.export_openapi"
    assert OPENAPI_PATH.exists(), f"{command} 로 docs/openapi.json 을 만든다"
    exported = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert exported == json.loads(render_openapi()), (
        f"API 가 바뀌었다. {command} 를 실행해 docs/openapi.json 을 갱신한다"
    )
