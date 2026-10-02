# 서비스 코드 분석 연동

서비스 생성 후 분석을 접수하고, 검증된 결과를 조회한 뒤 사용자가 실행 설정을 확인한다.
분석 작업은 배포 요청 없이 `service_analyses`에 저장되며 기존 배포 `jobs`의 상태를 바꾸지 않는다.
실제 이미지 빌드·배포와 로그 오류 진단은 각각 기존 담당 흐름이다.

## API

모든 경로는 `/api/v1/services/{service_id}` 아래이며 기존 쿠키/Bearer 인증과
`ApiResponse`를 사용한다. 없는 서비스와 다른 사용자의 서비스는 같은 404다.

| 메서드·경로 | 동작 |
| --- | --- |
| `POST /analysis` | `{ "mode": "opencode" }` 또는 명시적 `static`, 202 접수 |
| `GET /analysis` | 가장 최근 작업·진행·결과 조회. 미접수 시 404 `ANALYSIS_NOT_FOUND` |
| `POST /analysis/cancel` | `{ "analysisId": "UUID" }`, 대기/실행 취소. 이미 취소됐으면 같은 결과 |
| `POST /analysis/answers` | 결과 candidate 및 builder/port/commands를 명시 확인·저장 |

```json
{
  "analysisId": "<작업 UUID>",
  "serviceCandidateId": "<analysisResult.services[].serviceId>",
  "builder": "dockerfile",
  "dockerfilePath": "Dockerfile",
  "port": 8080,
  "startCommand": "node dist/index.js"
}
```

Dockerfile 경로는 **선택한 서비스 root 안의 상대 경로**다. Railpack에는 경로를 보내지 않는다.
생략한 port/buildCommand/startCommand는 기존 사용자 설정을 보존하고, 명시한 null은 비운다.
candidate root는 저장소 기준이며 여러 후보가 있으면 명시적으로 하나를 선택한다.
확인 직전에도 최신 SHA·서비스 저장소/브랜치/root·소유권/설치 접근을 검사한다.
확인된 같은 설정의 재전달은 멱등이고, 다른 설정을 확인하려면 새 분석이 필요하다.

실행 상태 `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED`와 분석 내용 상태
`complete/needs_input/unsupported`는 별개다. 정상적인 정보 부족 결과도 SUCCEEDED다.
확인 시 `confirmedAt`이 기록되며 unresolved 질문은 그대로 남는다.
`reviewRequired`는 분석의 추가 검토 필요 여부로 유지되고, `deploymentAuthorized`는 항상 false다.

## 저장·검증

WAS는 분석기 `1d9d2e38086b394d60fe90d889c6978357e35681`을 고정 설치한다.
비공개 저장소 인증에 의존하지 않도록 clean checkout에서 만든 wheel과 파일 digest를
`vendor/`에 묶고 설치된 패키지의 바이트도 manifest와 대조한다.
Worker는 전체 저장소를 분석해 workspace·Compose 관계를 보존한다. 서비스 root는 후보 선택 경계다.
GitHub App 외부 installation ID로 고정 SHA의 commit과 archive를 확인하고,
허용된 codeload redirect만 따라간다. App 토큰은 redirect 호스트로 전달하지 않는다.
소스 실행·checkout hook·프로젝트 OpenCode 설정 로딩은 하지 않는다.

원본 `.env`·인증 파일·링크를 제외/거절한다. 환경변수 예제는 분석기가 값을 마스킹하고
키/줄 근거로 사용한다. 압축 32 MiB·해제 100 MiB·2만 파일 상한을 적용한다.
임시 소스와 모델 원본 산출물은 실행 종료 후 삭제한다.

AnalysisResult v1, `iris.analysis-verification.v1`, readiness, deployment dossier,
안전하게 추린 실행 기록과 마스킹 근거를 DB에 보관한다. 결과 digest와 snapshot/context를 대조한다.
readiness는 추가 근거를 확보하므로 context hash가 달라도 같은 snapshot이면 정상이다.
모델 원본 요청/응답, 자격증명, Worker 절대 경로는 공개 응답에 포함하지 않는다.
중첩 unknown 필드의 `value:null`은 공통 응답의 null 생략 규칙과 별개로 보존한다.

Worker 선점은 SKIP LOCKED와 새 lease token을 사용한다. 취소·만료·재선점 뒤의 오래된 결과는
저장되지 않는다. 최대 3회 실행 후 재선점은 실패로 종료한다. 프로세스 종료 시 현재 lease만
대기로 반환하며 thread/모델 정리를 기다린 뒤 임시 자료를 지운다.
AI 모델 선택과 timeout·예산·서버 식별자는 접수 시 고정되고 변경되면 호출 전에 실패한다.

## 실행

```sh
uv sync --frozen --extra analysis
uv run --extra analysis alembic upgrade head
uv run --extra analysis uvicorn app.main:app --host 127.0.0.1 --port 8000
# 다른 프로세스에서
uv run --extra analysis python -m app.workers.analysis_worker
```

Control API와 Worker에 기존 DATABASE_URL/GitHub App 설정을 제공한다.
AI 접수에는 `.env.example`의 ANALYSIS_PROVIDER/MODEL과 API_KEY 또는 SERVER_URL이 필요하다.
모델 미설정은 503 `MODEL_NOT_CONFIGURED`로 답하고 static으로 자동 전환하지 않는다.
static은 별도 모델 런타임/키 없이 설치된 분석 패키지만 사용한다.

AI Worker는 고정 **OpenCode 1.18.33** 실행파일을 `ANALYSIS_EXECUTABLE`로 제공하거나,
같은 버전과 분석기 정책을 적용한 별도 서버를 `ANALYSIS_SERVER_URL`로 제공해야 한다.
Docker 이미지는 고정 Python 분석 패키지를 포함하지만 OpenCode 실행파일은 포함하지 않는다.
기본 Hive 설정은 검증된 `json_text`다. 외부 서버도 실제 버전·명세·모델·도구 정책 검사를 통과해야 한다.
출처와 실제 사용 모델 검증 실패를 무시하거나 다른 모델로 대체하지 않는다.

공유 ledger는 파일 잠금으로 예산을 예약한다. 여러 호스트에서는 공용 파일을 쓰지 않으면
예산이 합산되지 않는다. 영속 공유 경로와 플랫폼 예산 정책은 운영자가 구성한다.
이번 연결의 배포 계획은 deterministic policy 모드이며 추가 AI 계획 호출을 하지 않는다.
실제 image/네트워크/Secret 바인딩이 없는 실행 입력은 blocked 상태를 보존한다.

## 검증

```sh
uv run --extra analysis ruff check .
uv run --extra analysis ruff format --check .
uv run --extra analysis mypy app scripts
uv run --extra analysis pytest
# alembic upgrade head가 적용된 전용 PostgreSQL에서
TEST_DATABASE_URL=<전용 테스트 DB> uv run --extra analysis pytest -m integration
```

실제 고정 분석 패키지와 PostgreSQL을 사용하는 접수→Worker→결과→명시 확인 테스트,
동시 접수/선점, 만료 lease 회수, 오래된 결과 차단, 취소, timeout, 모델 선택 변경,
무결성 변조, GitHub mock archive/redirect/경로 검사, 중첩 null·기존 설정 보존을 포함한다.
테스트는 모델/클라우드 비용 없이 실행한다. GitHub 실제 권한 및 유료 AI 호출의 운영 E2E는
설정을 제공한 환경에서 별도로 검수한다. 이 연결은 `iris-was` develop API 계열에 추가됐으며,
main의 별도 Build/Deploy Worker 계열과 전체 병합을 수행하지 않는다.

결정 배경: [ADR 0011](adr/0011-service-analysis-jobs-and-explicit-confirmation.md).
