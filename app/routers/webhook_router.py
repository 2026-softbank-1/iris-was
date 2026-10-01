from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.dependencies import WebhookServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.webhook import WebhookReceiptResponse

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post(
    "/github",
    response_model=ApiResponse[WebhookReceiptResponse],
    response_model_exclude_none=True,
    summary="GitHub 웹훅 수신",
    responses=error_responses(401, 422, 503),
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "GitHub 가 보내는 이벤트 payload 원문(JSON). 서명 검증에 원문을 쓴다.",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def receive_github_webhook(
    request: Request,
    service: WebhookServiceDep,
    event: Annotated[str, Header(alias="X-GitHub-Event", description="이벤트 종류")],
    delivery_id: Annotated[
        str, Header(alias="X-GitHub-Delivery", description="전송 고유 ID. 멱등 키로 쓴다")
    ],
    signature: Annotated[
        str | None,
        Header(alias="X-Hub-Signature-256", description="본문의 HMAC-SHA256 (`sha256=<hex>`)"),
    ] = None,
) -> ApiResponse[WebhookReceiptResponse]:
    """인증은 로그인이 아니라 웹훅 서명으로 한다.

    `push` 는 연결된 브랜치의 자동 배포 서비스마다 배포 요청(trigger=PUSH)을 만든다.
    `installation` 의 created·deleted 는 GitHub App 설치 정보를 맞춘다. 그 밖의 이벤트는 무시한다.
    """
    receipt = await service.receive_github_event(
        event=event, delivery_id=delivery_id, signature=signature, body=await request.body()
    )
    return ApiResponse(data=receipt)
