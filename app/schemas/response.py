from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """API 입출력 스키마의 베이스. 서버 내부는 snake_case, JSON 은 camelCase 로 주고받는다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDetail(ApiModel):
    """필드별 검증 실패 사유. field 는 camelCase 필드 경로다."""

    field: str
    reason: str


class ApiResponse[T](ApiModel):
    """모든 JSON 응답의 공통 봉투. 성공·실패가 같은 구조를 쓰고, null 필드는 응답에서 뺀다.

    성공 시 code=None, 실패 시 도메인 예외의 code 를 담는다. details 는 검증 실패 시에만 채운다.
    """

    success: bool = True
    code: str | None = None
    message: str | None = None
    data: T | None = None
    details: list[ErrorDetail] | None = None


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page[T](ApiModel):
    """페이지 번호 기반 목록. page 는 0부터 시작한다."""

    items: list[T]
    total: int
    page: int
    size: int

    @classmethod
    def from_items(cls, items: list[T], page: int, size: int) -> "Page[T]":
        """메모리에 있는 전체 목록(외부 API 결과 등 DB 밖 목록)을 잘라 한 페이지로 만든다."""
        start = page * size
        return cls(items=items[start : start + size], total=len(items), page=page, size=size)


_ERROR_DESCRIPTIONS = {
    401: "로그인이 필요하다. 웹훅은 서명이 올바르지 않을 때 (UNAUTHORIZED)",
    403: "권한이 없다 (FORBIDDEN · REPOSITORY_NOT_ACCESSIBLE)",
    404: (
        "대상을 찾을 수 없다 "
        "(NOT_FOUND · PROJECT_NOT_FOUND · SERVICE_NOT_FOUND · DEPLOYMENT_REQUEST_NOT_FOUND)"
    ),
    409: (
        "현재 상태와 충돌한다. 이미 있는 이름이거나 진행 중인 배포가 있다 "
        "(CONFLICT · PROJECT_NAME_CONFLICT · SERVICE_NAME_CONFLICT · DEPLOYMENT_IN_PROGRESS)"
    ),
    422: "입력이 올바르지 않다 (VALIDATION_ERROR · INVALID_INPUT)",
    502: "외부 시스템(GitHub) 호출에 실패했다 (EXTERNAL_ERROR)",
    503: "필요한 설정이 없다 (NOT_CONFIGURED)",
}


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI 문서에 에러 응답을 싣는다. 실제 에러 본문은 예외 핸들러가 같은 봉투로 만든다."""
    return {
        status: {"model": ApiResponse[None], "description": _ERROR_DESCRIPTIONS[status]}
        for status in statuses
    }
