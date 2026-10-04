from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from app.enums import DatabaseEngine, FailureCode, ServiceKind
from app.schemas.response import ApiModel
from app.schemas.service import ServiceName
from app.services.database_engines import DEFAULT_STORAGE_GI, MAX_STORAGE_GI, MIN_STORAGE_GI
from app.services.stack_service import StackServiceView, StackView


class CreateDatabaseRequest(ApiModel):
    """개발·데모용 관리형 DB(단일 인스턴스, 백업 없음). 서비스를 지우면 데이터도 지워진다."""

    name: ServiceName = Field(description="서비스 이름(DNS 레이블)", examples=["postgres"])
    engine: DatabaseEngine = Field(examples=["postgres"])
    storage_gi: int | None = Field(
        default=None,
        ge=MIN_STORAGE_GI,
        le=MAX_STORAGE_GI,
        description=f"디스크 GiB. 기본 {DEFAULT_STORAGE_GI}. 만든 뒤에는 바꿀 수 없다.",
        examples=[5],
    )
    target_ids: list[int] | None = Field(
        default=None, description="배포 타깃 id. 정확히 1개다. 생략하면 `aws` 다(on-prem 은 422)."
    )


class StackServiceResponse(ApiModel):
    service_id: int
    name: str
    unit_id: str | None = None
    kind: ServiceKind
    order: int = Field(description="배포 순서. 1 = DB, 2 = DB 를 쓰는 앱, 3 = 그 앱을 쓰는 앱 …")
    depends_on: list[str] = Field(description="이 서비스가 쓰는 같은 스택의 unitId")
    status: str = Field(
        description=(
            "배포 상태(deployment_status) 또는 HELD(앞 단계 실패로 시작 안 함)·NOT_DEPLOYED."
            " 앞 단계를 기다리는 동안은 QUEUED 이고 waitingFor 가 있다."
        ),
        examples=["SUCCEEDED"],
    )
    deployment_id: int | None = None
    held_by: str | None = Field(default=None, description="HELD 일 때 막은 앞 단계 unitId")
    waiting_for: list[str] | None = Field(
        default=None, description="QUEUED 로 기다리는 앞 단계 unitId"
    )
    failure_code: FailureCode | None = None

    @classmethod
    def from_view(cls, view: StackServiceView) -> "StackServiceResponse":
        return cls(
            service_id=view.service.id,
            name=view.service.name,
            unit_id=view.service.stack_unit_id,
            kind=view.service.kind,
            order=view.order,
            depends_on=view.depends_on_unit_ids,
            status=view.status,
            deployment_id=view.deployment_request.id if view.deployment_request else None,
            held_by=view.held_by_unit_id,
            waiting_for=view.waiting_for_unit_ids or None,
            failure_code=view.failure_code,
        )


class StackChangeResponse(ApiModel):
    type: str = Field(
        description="UNIT_ADDED·UNIT_REMOVED·UNIT_CHANGED·DEPENDENCY_ADDED·DEPENDENCY_REMOVED"
    )
    unit_id: str
    field: str | None = None
    from_: Any = Field(default=None, alias="from")
    to: Any = None


class StackPendingChangesResponse(ApiModel):
    analysis_id: int = Field(description="이 분석을 apply 하면 증분 적용된다")
    source_sha: str | None = None
    detected_at: datetime
    changes: list[StackChangeResponse]


class StackResponse(ApiModel):
    id: int
    project_id: int
    repository_url: str
    source_branch: str
    root_directory: str | None = None
    analysis_id: int = Field(description="스택의 기준(마지막으로 apply 한) 분석")
    services: list[StackServiceResponse] = Field(description="배포 순서(order)대로")
    is_deploying: bool
    latest_stack_deployment_id: int | None = None
    pending_changes: StackPendingChangesResponse | None = Field(
        default=None, description="푸시 재분석이 기준과 다르면 있다. 그 분석을 apply 하면 지워진다."
    )

    @classmethod
    def from_view(cls, view: StackView) -> "StackResponse":
        stack = view.stack
        pending = stack.pending_changes
        return cls(
            id=stack.id,
            project_id=stack.project_id,
            repository_url=stack.source_repository_url,
            source_branch=stack.source_branch,
            root_directory=stack.root_directory,
            analysis_id=stack.analysis_id,
            services=[StackServiceResponse.from_view(s) for s in view.services],
            is_deploying=view.is_deploying,
            latest_stack_deployment_id=(
                view.latest_deployment.id if view.latest_deployment is not None else None
            ),
            pending_changes=(
                StackPendingChangesResponse.model_validate(pending) if pending else None
            ),
        )


class CreateStackDeploymentRequest(ApiModel):
    service_ids: list[int] | None = Field(
        default=None,
        description=(
            "다시 배포할 서비스. 생략하면 스택 전체(이미 떠 있는 DB 는 다시 띄우지 않는다)."
            " 의존 순서(DB → 앱 → 나머지)로 앞 단계가 성공한 뒤 다음 단계가 시작한다."
        ),
    )
    skip_variable_validation: bool = Field(
        default=False,
        description="true 면 환경변수 검증 error 가 있어도 배포한다(오탐 우회). 기본은 422.",
    )


StackIdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=64)]
