"""AI 에러 진단. 에이전트(iris-error-check-agent)에 보내는 요청과 받은 결과, API 응답을 정의한다.

에이전트 쪽 필드는 요청이 camelCase, 결과가 snake_case 다. 결과는 ApiModel 로 읽어
(populate_by_name) API 로 내보낼 때 camelCase 로 바꾼다. 모르는 필드는 버린다.
"""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field

from app.enums import DeploymentStatus, DiagnosisStatus
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.schemas.response import ApiModel

FailedStage = Literal["build", "release", "deploy", "runtime", "unknown"]


class AgentLogEvent(ApiModel):
    id: str
    timestamp: datetime
    stage: Literal["build", "runtime"]
    source_id: str = Field(description="로그가 나온 곳(Pod 이름 등)")
    stream: Literal["stdout", "stderr", "combined"] | None = None
    sequence: int = Field(description="같은 출처 안의 순서. 1부터 시작한다.")
    text: str


class AgentLogRange(ApiModel):
    from_: datetime = Field(alias="from")
    to: datetime
    is_complete: bool = Field(description="조회 범위 안의 로그를 빠짐없이 담았으면 true")


class AgentSource(ApiModel):
    format: Literal["tar.gz"] = "tar.gz"
    download_url: str = Field(description="S3 presigned URL. 로그·응답에 남기지 않는다.")
    expires_at: datetime
    commit_sha: str | None = Field(
        default=None, description="40자리 커밋 SHA. 모르면 보내지 않는다."
    )
    root_directory: str = "."


class AgentDiagnoseData(ApiModel):
    """에이전트 `POST /diagnose` 요청 본문. 확인할 수 없는 값은 보내지 않는다(null)."""

    project_id: int
    service_id: int
    deployment_id: int
    attempt_id: int | None = Field(default=None, description="빌드 시도 횟수(builds.attempt)")
    deployment_status: DeploymentStatus
    failed_stage: FailedStage | None = None
    exit_code: int | None = None
    log_range: AgentLogRange
    logs: list[AgentLogEvent]
    source: AgentSource | None = None


class DiagnosisObservation(ApiModel):
    id: str
    kind: Literal["failure", "context"]
    text: str
    evidence_ids: list[str]


class DiagnosisHypothesis(ApiModel):
    """원인 후보. 근거 로그(evidence_ids)와 반대 근거, 불확실한 점을 함께 준다."""

    id: str
    category: str = Field(
        description=(
            "configuration · dependency · build_compile · start_command · port_binding · "
            "external_connection · access_permission · resource · health_check · "
            "release_migration · other"
        )
    )
    support_level: Literal["direct", "supported"]
    statement: str
    observation_ids: list[str]
    evidence_ids: list[str]
    counter_evidence_ids: list[str] = []
    uncertainty: str


class DiagnosisCheck(ApiModel):
    id: str
    target: str
    method: str
    purpose: str
    hypothesis_ids: list[str] = []


class DiagnosisMissingInformation(ApiModel):
    requested_data: str
    reason: str


class RemediationPlaceholder(ApiModel):
    name: str
    description: str


class RemediationChange(ApiModel):
    kind: Literal["code", "configuration", "command"]
    target: str
    target_known: bool = Field(
        description="수정 대상 문자열이 로그에 있었는지. 맞다는 보장은 아니다."
    )
    instruction: str
    language: str
    snippet_kind: str = Field(description="항상 template. 자리표시자를 채워 써야 한다.")
    snippet: str
    placeholders: list[RemediationPlaceholder] = []


class RemediationVerification(ApiModel):
    instruction: str
    expected_result: str


class RemediationPlan(ApiModel):
    """해결안. 제안일 뿐 서버가 실행하지 않는다."""

    id: str
    title: str
    hypothesis_ids: list[str]
    evidence_ids: list[str]
    apply_when: list[str]
    changes: list[RemediationChange]
    verification: list[RemediationVerification]
    rollback: list[str]
    risks: list[str]


class Remediation(ApiModel):
    status: Literal["proposed", "needs_more_evidence", "not_needed"]
    reason: str
    plans: list[RemediationPlan] = []


class DiagnosisAnalysis(ApiModel):
    """진단 내용. 원인은 hypotheses, 해결책은 remediation.plans 다."""

    analysis_status: Literal["diagnosed", "insufficient_evidence", "no_failure_evidence"]
    summary: str
    observations: list[DiagnosisObservation] = []
    hypotheses: list[DiagnosisHypothesis] = []
    next_checks: list[DiagnosisCheck] = []
    missing_information: list[DiagnosisMissingInformation] = []
    limitations: list[str] = []
    remediation: Remediation


class EvidenceLine(ApiModel):
    """진단이 근거로 쓴 로그 한 줄(마스킹됨). 원인·해결안의 evidence_ids 가 가리킨다."""

    id: str
    source_id: str
    stage: str
    stream: str | None = None
    timestamp: str | None = None
    event_line: int | None = None
    text: str


class SourceFinding(ApiModel):
    path: str
    start_line: int
    end_line: int
    explanation: str
    evidence_ids: list[str]
    source_evidence_ids: list[str]
    hypothesis_ids: list[str]


class SourceAnalysis(ApiModel):
    """소스 코드까지 본 결과. 소스가 없거나 읽지 못해도 로그 진단은 유지된다."""

    status: Literal["not_needed", "unavailable", "analyzed", "failed"]
    reason: str
    commit_sha: str | None = None
    findings: list[SourceFinding] = []
    evidence: list[dict[str, Any]] = Field(
        default=[], description="findings 의 sourceEvidenceIds 가 가리키는 코드 조각"
    )
    limitations: list[str] = []
    error: dict[str, Any] | None = None


class AgentDiagnosisResult(ApiModel):
    """에이전트 응답(diagnosis-result.v3) 중 이 서버가 쓰는 부분."""

    job_status: Literal["succeeded", "failed", "timed_out"]
    analysis: DiagnosisAnalysis | None = None
    evidence: list[EvidenceLine] = []
    source_analysis: SourceAnalysis | None = None
    input_limitations: list[str] = []
    error: dict[str, Any] | None = None


class DiagnosisResponse(ApiModel):
    """AI 진단 1회. 배포 요청의 상태는 진단으로 바뀌지 않는다."""

    id: int
    deployment_id: int
    status: DiagnosisStatus = Field(
        description="RUNNING 이면 진단 중, FAILED 면 errorCode 를 본다."
    )
    error_code: str | None = Field(
        default=None,
        description=(
            "FAILED 일 때만 있다. 이 서버의 코드(DIAGNOSIS_LOGS_UNAVAILABLE 등)이거나 "
            "에이전트의 코드(MODEL_TIMEOUT 등)다."
        ),
    )
    analysis: DiagnosisAnalysis | None = Field(
        default=None, description="SUCCEEDED 일 때만 있다. 원인(hypotheses)과 해결책(remediation)"
    )
    evidence: list[EvidenceLine] = []
    source_analysis: SourceAnalysis | None = None
    input_limitations: list[str] = Field(
        default=[], description="로그 누락·마스킹·잘림 등 진단 입력의 한계"
    )
    created_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_model(cls, diagnosis: DeploymentDiagnosis) -> Self:
        result = (
            AgentDiagnosisResult.model_validate(diagnosis.result)
            if diagnosis.result is not None
            else None
        )
        return cls(
            id=diagnosis.id,
            deployment_id=diagnosis.deployment_request_id,
            status=diagnosis.status,
            error_code=diagnosis.error_code,
            analysis=result.analysis if result else None,
            evidence=result.evidence if result else [],
            source_analysis=result.source_analysis if result else None,
            input_limitations=result.input_limitations if result else [],
            created_at=diagnosis.created_at,
            finished_at=diagnosis.finished_at,
        )
