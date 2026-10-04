from enum import StrEnum


class JobKind(StrEnum):
    BUILD = "BUILD"
    DEPLOY = "DEPLOY"
    RECONCILE = "RECONCILE"
    ROLLBACK = "ROLLBACK"
    REMOVE = "REMOVE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class Builder(StrEnum):
    DOCKERFILE = "dockerfile"
    RAILPACK = "railpack"


class Environment(StrEnum):
    PROD = "prod"


class DeploymentStatus(StrEnum):
    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    # 진행 중에 더 새로운 요청이 대신해 중단됐다. Worker 가 cancel_requested_at 을 보고 끝낸다.
    SUPERSEDED = "SUPERSEDED"


# 서비스·환경별로 동시에 하나만 허용하는 "진행 중" 상태.
ACTIVE_DEPLOYMENT_STATUSES = (
    DeploymentStatus.QUEUED,
    DeploymentStatus.BUILDING,
    DeploymentStatus.DEPLOYING,
)

# 앱 컨테이너가 listen 하는 포트. Deploy Worker 가 chart 에 넘기고 `PORT` 환경변수로 주입된다.
APP_PORT = 8080


class DeploymentTrigger(StrEnum):
    MANUAL = "MANUAL"
    PUSH = "PUSH"
    CLI = "CLI"
    REDEPLOY = "REDEPLOY"
    ROLLBACK = "ROLLBACK"
    RESTART = "RESTART"
    REMOVE = "REMOVE"


class DeploymentStrategy(StrEnum):
    """새 Pod 로 바꾸는 방식. 단계·대기 시간은 iris-service chart 가 정한다."""

    ROLLING = "ROLLING"
    CANARY = "CANARY"
    BLUE_GREEN = "BLUE_GREEN"


class BuildStatus(StrEnum):
    PENDING = "PENDING"
    SNAPSHOTTING = "SNAPSHOTTING"
    BUILDING = "BUILDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReleaseStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class FailureCode(StrEnum):
    SOURCE_NOT_ACCESSIBLE = "SOURCE_NOT_ACCESSIBLE"
    SOURCE_REF_NOT_FOUND = "SOURCE_REF_NOT_FOUND"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    # 올린 소스 아카이브가 손상됐거나 허용되지 않는 항목(경로 이탈·장치 파일 등)을 담고 있다.
    SOURCE_INVALID = "SOURCE_INVALID"
    BUILD_CONFIG_REQUIRED = "BUILD_CONFIG_REQUIRED"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_TIMED_OUT = "BUILD_TIMED_OUT"
    BUILD_INFRA_ERROR = "BUILD_INFRA_ERROR"
    DEPLOY_FAILED = "DEPLOY_FAILED"
    DEPLOY_TIMED_OUT = "DEPLOY_TIMED_OUT"
    DEPLOY_INFRA_ERROR = "DEPLOY_INFRA_ERROR"
    # 배포 전 환경변수 검증에서 error 가 나왔다. 푸시 자동 배포만 이 코드로 요청을 남긴다(빌드 안
    # 함).
    VARIABLES_INVALID = "VARIABLES_INVALID"
    # 스택 배포에서 이 서비스가 기다리던 앞 단계(DB·의존 앱) 배포가 실패해 시작하지 않았다(보류).
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"


# 빌드·배포를 시작하기 전에 끝난 실패. 로그가 없어 자동 진단하지 않는다.
PRE_EXECUTION_FAILURE_CODES = (FailureCode.VARIABLES_INVALID, FailureCode.DEPENDENCY_FAILED)


class DiagnosisStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TargetKind(StrEnum):
    AWS = "AWS"
    ONPREM = "ONPREM"


class CliLoginSessionStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    # 만료됐거나 토큰을 이미 내줬다. 토큰을 내준 세션은 consumed_at 이 채워진다.
    EXPIRED = "EXPIRED"


class RepositoryAnalysisStatus(StrEnum):
    """레포 구성 분석(Analysis Gate) 상태. APPLIED 는 분석 결과로 서비스를 만든 뒤다."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    APPLIED = "APPLIED"


class AnalysisGateMode(StrEnum):
    """auto 는 단순 레포면 분석을 생략하고, force 는 단순해도 배포 단위를 분석한다."""

    AUTO = "auto"
    FORCE = "force"


class AnalysisGateDecision(StrEnum):
    SKIP = "skip"
    ANALYZE = "analyze"


class AnalysisGateComplexity(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"
    UNSUPPORTED = "unsupported"


class AnalysisErrorCode(StrEnum):
    """분석이 실패한 이유. 웹은 이 값과 상관없이 "분석 없이 단일 서비스로 생성" 을 제공한다."""

    SOURCE_NOT_ACCESSIBLE = "SOURCE_NOT_ACCESSIBLE"
    SOURCE_REF_NOT_FOUND = "SOURCE_REF_NOT_FOUND"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    SOURCE_INVALID = "SOURCE_INVALID"
    ANALYZER_UNAVAILABLE = "ANALYZER_UNAVAILABLE"
    ANALYZER_TIMED_OUT = "ANALYZER_TIMED_OUT"
    ANALYZER_FAILED = "ANALYZER_FAILED"
    # Worker 가 처리 중에 여러 번 죽어 lease 만 남았다.
    ANALYSIS_INTERRUPTED = "ANALYSIS_INTERRUPTED"


class ServiceKind(StrEnum):
    """APP 은 소스를 빌드해 띄우는 앱, DATABASE 는 빌드 없이 고정 공식 이미지로 띄우는 관리형 DB."""

    APP = "APP"
    DATABASE = "DATABASE"


class DatabaseEngine(StrEnum):
    """관리형 DB 엔진. 분석기(`dependencies[].engine`)·chart(`database.engine`) 표기 그대로
    소문자다."""

    POSTGRES = "postgres"
    MYSQL = "mysql"
    MONGODB = "mongodb"
    REDIS = "redis"


class ReferenceProperty(StrEnum):
    """참조 변수가 가리키는 다른 서비스의 연결 정보. 앱은 url·host·port 만 있다."""

    URL = "url"
    HOST = "host"
    PORT = "port"
    USER = "user"
    PASSWORD = "password"
    DATABASE = "database"


class VariableIssueCode(StrEnum):
    REQUIRED_MISSING = "REQUIRED_MISSING"
    LOCALHOST_ADDRESS = "LOCALHOST_ADDRESS"
    UNRESOLVABLE_HOST = "UNRESOLVABLE_HOST"
    SCHEME_MISMATCH = "SCHEME_MISMATCH"
    REFERENCE_BROKEN = "REFERENCE_BROKEN"


class VariableIssueSeverity(StrEnum):
    """error 는 배포 요청을 막고(422 VARIABLES_INVALID), warning 은 알리기만 한다."""

    ERROR = "error"
    WARNING = "warning"


class StackDeploymentStepStatus(StrEnum):
    """스택 배포 한 단계. WAITING 은 앞 단계 성공을 기다리는 중(배포 요청 QUEUED, job 없음),
    STARTED 는 첫 job 을 만들었다(이후 진행은 배포 요청 상태), HELD 는 앞 단계가 실패해 시작하지
    않았다.
    """

    WAITING = "WAITING"
    STARTED = "STARTED"
    HELD = "HELD"


class OnpremServerStatus(StrEnum):
    """사용자가 등록한 온프레미스 서버의 연결 상태."""

    # 등록만 했다. 서버에서 설치 명령을 아직 실행하지 않았다.
    PENDING = "PENDING"
    # 서버가 connect 를 보냈다. Deploy Worker 가 GitOps 반영·연결 확인 중이다.
    REGISTERING = "REGISTERING"
    # probe Application 이 Synced+Healthy 다. 배포할 수 있다.
    CONNECTED = "CONNECTED"
    # 기한 안에 연결되지 않았거나 GitOps 반영에 실패했다. 토큰을 다시 받아 명령을 다시 실행한다.
    FAILED = "FAILED"


class OnpremServerConnectionStatus(StrEnum):
    """API 가 알리는 서버 상태. 저장한 상태에 하트비트로 계산한 DISCONNECTED 를 더한다.

    DISCONNECTED 는 저장하지 않는다. CONNECTED 서버의 하트비트(last_seen_at)가 끊긴 동안만 보이고,
    서버가 다시 부르면 저절로 CONNECTED 로 돌아온다.
    """

    PENDING = "PENDING"
    REGISTERING = "REGISTERING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


class OnpremServerFailureCode(StrEnum):
    # probe Application 이 기한 안에 Synced+Healthy 가 되지 않았다.
    CONNECT_TIMED_OUT = "CONNECT_TIMED_OUT"
    # 서버 values 를 GitOps 저장소에 커밋하지 못하고 재시도를 소진했다.
    GITOPS_COMMIT_FAILED = "GITOPS_COMMIT_FAILED"


class ConsoleUnavailableReason(StrEnum):
    """서비스 콘솔을 열 수 없는 사유(API 응답 값, 저장하지 않는다). 판정 순서는 선언 순서와 다르다:
    타깃 종류 → 설정 → 떠 있는 release."""

    # 그 타깃에 떠 있는(lastKnownGood) release 가 없다.
    NO_RUNNING_DEPLOYMENT = "NO_RUNNING_DEPLOYMENT"
    # 온프레미스 타깃은 아직 지원하지 않는다(ADR 0033).
    TARGET_NOT_SUPPORTED = "TARGET_NOT_SUPPORTED"
    # Control API 에 콘솔 설정(ticket 서명키·Gateway 주소)이 없다.
    NOT_CONFIGURED = "NOT_CONFIGURED"


class ConsoleErrorCode(StrEnum):
    """Console Gateway 가 내보내는 오류 코드. REST 오류 본문의 code 이자 WebSocket `error` 프레임의
    code 다. 저장하지 않는다."""

    UNAUTHORIZED = "UNAUTHORIZED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_REUSED = "TOKEN_REUSED"
    POD_NOT_FOUND = "POD_NOT_FOUND"
    POD_NOT_READY = "POD_NOT_READY"
    SHELL_NOT_FOUND = "SHELL_NOT_FOUND"
    SESSION_LIMIT_EXCEEDED = "SESSION_LIMIT_EXCEEDED"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    MAX_DURATION_EXCEEDED = "MAX_DURATION_EXCEEDED"
    CLUSTER_UNAVAILABLE = "CLUSTER_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
