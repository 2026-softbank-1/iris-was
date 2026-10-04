from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from app.enums import ReferenceProperty, VariableIssueCode, VariableIssueSeverity
from app.schemas.response import ApiModel
from app.services.variable_references import VariableReference
from app.services.variable_service import (
    MAX_KEY_LENGTH,
    MAX_VALUE_LENGTH,
    ServiceVariables,
    SystemVariable,
    VariableEntry,
)
from app.services.variable_validation import VariableIssue, VariableValidation

# 키 형식·예약어 검사는 Service 가 한다(Raw 편집기 경로와 같은 규칙). 여기선 요청 크기만 막는다.
_Key = Annotated[str, StringConstraints(min_length=1, max_length=MAX_KEY_LENGTH)]
_Value = Annotated[str, StringConstraints(max_length=MAX_VALUE_LENGTH)]
_RAW_MAX_LENGTH = 1024 * 1024


class VariableReferenceSchema(ApiModel):
    """같은 프로젝트 다른 서비스의 연결 정보. 배포 직전에 그 서비스의 지금 값으로 풀린다."""

    service_id: int = Field(gt=0, examples=[12])
    property: ReferenceProperty = Field(
        description=(
            "DB: url·host·port·user·password·database(redis 는 database 없음). 앱: url·host·port"
        ),
        examples=["url"],
    )

    def to_reference(self) -> VariableReference:
        return VariableReference(service_id=self.service_id, property=self.property)

    @classmethod
    def from_reference(cls, reference: VariableReference) -> "VariableReferenceSchema":
        return cls(service_id=reference.service_id, property=reference.property)


class _ValueOrReference(ApiModel):
    value: _Value | None = Field(
        default=None,
        description="빈 문자열도 허용한다. reference 와 함께 보내지 않는다.",
        examples=["postgres://..."],
    )
    reference: VariableReferenceSchema | None = Field(
        default=None,
        description=(
            "값 대신 다른 서비스의 연결 정보를 가리킨다. 같은 프로젝트의 다른 서비스여야 하고"
            " 기능이 켜진 AWS 타깃에서만 쓴다(아니면 422)."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "_ValueOrReference":
        if (self.value is None) == (self.reference is None):
            raise ValueError("exactly one of value or reference is required")
        return self


class VariableCreateRequest(_ValueOrReference):
    """변수 하나를 만든다. 키는 영문·숫자·밑줄이고 숫자로 시작하지 않는다. 값과 참조 중 하나다."""

    key: _Key = Field(examples=["DATABASE_URL"])


class VariableUpdateRequest(_ValueOrReference):
    """값 변수를 참조로, 참조를 값으로 바꿀 수도 있다."""


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
    value: str | None = Field(
        default=None, description="값 변수의 평문(소유자에게만). 참조 변수는 없다."
    )
    reference: VariableReferenceSchema | None = None
    resolved: str | None = Field(
        default=None,
        description="참조 변수가 배포 때 들어갈 값의 미리보기. 비밀번호는 `****` 로 가린다."
        " 대상이 없어졌으면 없다.",
        examples=["postgresql://app:****@app.svc-12.svc.cluster.local:5432/app"],
    )

    @classmethod
    def from_entry(cls, entry: VariableEntry) -> "VariableResponse":
        return cls(
            key=entry.key,
            value=entry.value,
            reference=(
                VariableReferenceSchema.from_reference(entry.reference)
                if entry.reference is not None
                else None
            ),
            resolved=entry.resolved,
        )


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


class VariableSuggestionResponse(ApiModel):
    reference: VariableReferenceSchema


class VariableIssueResponse(ApiModel):
    key: str
    severity: VariableIssueSeverity = Field(description="error 는 배포 요청을 막는다(422)")
    code: VariableIssueCode
    message: str
    suggestion: VariableSuggestionResponse | None = Field(
        default=None, description="같은 프로젝트에 맞는 서비스가 있으면 그 참조로 바꾸자는 제안"
    )

    @classmethod
    def from_issue(cls, issue: VariableIssue) -> "VariableIssueResponse":
        return cls(
            key=issue.key,
            severity=issue.severity,
            code=issue.code,
            message=issue.message,
            suggestion=(
                VariableSuggestionResponse(
                    reference=VariableReferenceSchema.from_reference(issue.suggestion)
                )
                if issue.suggestion is not None
                else None
            ),
        )


class VariablesValidationResponse(ApiModel):
    ok: bool = Field(description="error 가 없으면 true (warning 은 있어도 된다)")
    issues: list[VariableIssueResponse]

    @classmethod
    def from_validation(cls, validation: VariableValidation) -> "VariablesValidationResponse":
        return cls(
            ok=validation.ok,
            issues=[VariableIssueResponse.from_issue(i) for i in validation.issues],
        )
