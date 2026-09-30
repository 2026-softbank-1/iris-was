from app.schemas.response import ApiModel, ApiResponse, ErrorDetail, Page


class DeploymentSummary(ApiModel):
    deployment_request_id: int


def test_api_response_page_serializes_camel_case_without_nulls() -> None:
    page = Page(items=[DeploymentSummary(deployment_request_id=1)], total=1, page=0, size=20)

    body = ApiResponse(data=page).model_dump(by_alias=True, exclude_none=True)

    assert body == {
        "success": True,
        "data": {"items": [{"deploymentRequestId": 1}], "total": 1, "page": 0, "size": 20},
    }


def test_api_response_validation_error_serializes_same_envelope() -> None:
    response: ApiResponse[None] = ApiResponse(
        success=False,
        code="VALIDATION_ERROR",
        message="invalid request",
        details=[ErrorDetail(field="sourceSha", reason="required")],
    )

    body = response.model_dump(by_alias=True, exclude_none=True)

    assert body == {
        "success": False,
        "code": "VALIDATION_ERROR",
        "message": "invalid request",
        "details": [{"field": "sourceSha", "reason": "required"}],
    }
