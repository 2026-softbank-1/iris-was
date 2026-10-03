from typing import Annotated

from pydantic import Field, StringConstraints

from app.schemas.response import ApiModel
from app.services.variable_service import (
    MAX_KEY_LENGTH,
    MAX_VALUE_LENGTH,
    ServiceVariables,
    SystemVariable,
    VariableEntry,
)

# 키 형식·예약어 검사는 Service 가 한다(Raw 편집기 경로와 같은 규칙). 여기선 요청 크기만 막는다.
_Key = Annotated[str, StringConstraints(min_length=1, max_length=MAX_KEY_LENGTH)]
_Value = Annotated[str, StringConstraints(max_length=MAX_VALUE_LENGTH)]
_RAW_MAX_LENGTH = 1024 * 1024


class VariableCreateRequest(ApiModel):
    """변수 하나를 만든다. 키는 영문·숫자·밑줄이고 숫자로 시작하지 않는다."""

    key: _Key = Field(examples=["DATABASE_URL"])
    value: _Value = Field(description="빈 문자열도 허용한다.", examples=["postgres://..."])


class VariableUpdateRequest(ApiModel):
    value: _Value = Field(description="빈 문자열도 허용한다.", examples=["postgres://..."])


class VariablesRawRequest(ApiModel):
    """`KEY=VALUE` 한 줄에 하나. `.env` 형식이고 큰따옴표 값은 JSON 이스케이프를 따른다.

    따옴표 값은 닫는 따옴표까지 여러 줄에 걸칠 수 있다(PEM 개인키 등).
    여러 줄 값의 줄바꿈은 CRLF 여도 LF 하나로 읽는다.
    """

    raw: Annotated[str, StringConstraints(max_length=_RAW_MAX_LENGTH)] = Field(
        description=(
            "서비스의 변수 전체를 이 텍스트로 바꾼다. 텍스트에 없는 키는 지워진다. "
            "빈 줄과 `#` 주석은 무시한다. 하나라도 받을 수 없으면 422 `INVALID_INPUT` 이고 "
            "아무것도 바뀌지 않는다. `details` 에 줄 번호와 사유가 있다"
            "(예: `line 7: reserved key PORT`). 값은 담지 않는다."
        ),
        examples=['DATABASE_URL="postgres://..."\nLOG_LEVEL=info'],
    )


class VariableResponse(ApiModel):
    key: str
    value: str

    @classmethod
    def from_entry(cls, entry: VariableEntry) -> "VariableResponse":
        return cls(key=entry.key, value=entry.value)


class SystemVariableResponse(ApiModel):
    """플랫폼이 배포할 때 앱에 주입하는 변수. 사용자가 바꿀 수 없다."""

    key: str = Field(examples=["PORT"])
    description: str
    value: str | None = Field(
        default=None,
        description="서비스만으로 값이 정해지는 변수만 있다. 타깃·배포마다 다른 값은 없다.",
        examples=["8080"],
    )

    @classmethod
    def from_system_variable(cls, variable: SystemVariable) -> "SystemVariableResponse":
        return cls(key=variable.key, description=variable.description, value=variable.value)


class ServiceVariablesResponse(ApiModel):
    variables: list[VariableResponse] = Field(description="사용자가 등록한 변수. 키 순이다.")
    system_variables: list[SystemVariableResponse] = Field(
        description="플랫폼이 자동으로 주입하는 변수. 같은 이름의 사용자 변수는 만들 수 없다."
    )

    @classmethod
    def from_service_variables(cls, result: ServiceVariables) -> "ServiceVariablesResponse":
        return cls(
            variables=[VariableResponse.from_entry(v) for v in result.variables],
            system_variables=[
                SystemVariableResponse.from_system_variable(v) for v in result.system_variables
            ],
        )
