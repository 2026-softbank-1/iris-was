from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from starlette.requests import ClientDisconnect

from app.core.exceptions import InvalidInputError
from app.dependencies import CurrentUserDep, UploadServiceDep
from app.schemas.response import ApiResponse, error_responses
from app.schemas.upload import UploadResponse

router = APIRouter(prefix="/api/v1", tags=["uploads"])

_ARCHIVE_BODY_DOC = {
    "requestBody": {
        "required": True,
        "description": (
            "`tar` 를 gzip 으로 압축한 아카이브 바이트 그대로(multipart 아님). "
            "서비스 소스의 루트가 아카이브의 루트이고 최상위에 파일이 바로 있다."
        ),
        "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}},
    }
}


async def _read_body(request: Request) -> AsyncIterator[bytes]:
    try:
        async for chunk in request.stream():
            yield chunk
    except ClientDisconnect as exc:
        # 클라이언트가 끊었다. 응답은 닿지 않지만 500 으로 기록되지 않게 도메인 오류로 바꾼다.
        raise InvalidInputError("request body was interrupted") from exc


@router.post(
    "/services/{service_id}/uploads",
    response_model=ApiResponse[UploadResponse],
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="소스 아카이브 업로드 (likelion up)",
    description=(
        "로컬 폴더를 묶은 tar.gz 를 올린다. 본문이 곧 아카이브다. 업로드는 24시간 안에 "
        "`POST /services/{serviceId}/deployments` 의 `CLI` 트리거로 배포 요청 하나에만 쓸 수 있다. "
        "서비스 소유자만 올릴 수 있고, 아카이브의 루트는 서비스 `rootDirectory` 와 상관없이 "
        "서비스 소스의 루트로 본다. 아카이브 안의 경로·링크 검사는 빌드 때 한다."
    ),
    responses=error_responses(401, 404, 413, 415, 422, 502, 503),
    openapi_extra=_ARCHIVE_BODY_DOC,
)
async def create_upload(
    service_id: int,
    request: Request,
    user: CurrentUserDep,
    upload_service: UploadServiceDep,
    content_length: Annotated[
        int | None,
        Header(
            description="본문 바이트 수. 없으면 422, 한도를 넘으면 읽기 전에 413 으로 거절한다."
        ),
    ] = None,
) -> ApiResponse[UploadResponse]:
    upload = await upload_service.create_upload(
        user.id, service_id, content_length=content_length, body=_read_body(request)
    )
    return ApiResponse(data=UploadResponse.from_model(upload))
