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


class Page[T](ApiModel):
    """페이지 번호 기반 목록. page 는 0부터 시작한다."""

    items: list[T]
    total: int
    page: int
    size: int
