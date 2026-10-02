# 분석 에이전트 · WAS · 웹 원클릭 연결

`저장소 → 분석 → 부족 정보 확인 → 분석 에이전트 계획 → Dockerfile/Railpack 빌드 → 배포 → 실패 로그 진단`을 실행한다. 단독 분석은 기존 `/analysis` API를 그대로 쓴다. 배포 전 분석을 생략하지 않는다.

## API와 진행

모든 경로는 `/api/v1/services/{serviceId}`이며 로그인한 소유자만 접근한다.

| 요청 | 기능 |
|---|---|
| `POST /pipelines` | `{mode:"opencode",autoDeploy:true,enableAutoDeploy:true}` 접수, 202. `Idempotency-Key` 지원 |
| `GET /pipelines` | 최신 작업·분석 ID·질문·두 종류 계획·배포 ID·상태 조회 |
| `POST /pipelines/{id}/answers` | candidate/builder/Dockerfile/port/commands/variables 입력 후 계획 재개 |
| `POST /pipelines/{id}/cancel` | 분석·질문·계획 단계 취소. 빌드·배포 이후 409 |
| `POST·GET /deployments/{id}/diagnosis` | 실패한 실행 회차 진단 접수·조회. 자동 실패 진단과 같은 작업 |

상태는 `QUEUED → ANALYZING → AWAITING_INPUT → PLANNING → BUILDING → DEPLOYING → SUCCEEDED`다. 정보가 충분하면 질문 단계를 생략한다. 계획 미리보기 `autoDeploy:false`는 `SUCCEEDED/plan_ready`로 끝나고 빌드하지 않는다. 이를 실행하려면 새 파이프라인을 요청한다.

Create 및 Analyze & deploy 버튼은 이 API를 사용한다. 서비스/프로젝트·분석 결과·원문 근거·기존 설정 UI를 유지하고 서버 상태를 폴링해 새로고침 후에도 복구한다. 관리된 서비스의 이후 push는 즉시 BUILD를 만들지 않고 같은 분석 파이프라인을 큐에 넣는다. 새 head가 이전 큐 항목을 대체했으면 `PIPELINE_SUPERSEDED`로 끝낸다.

`opencode`가 기본이며 모델·런타임 미설정은 503, 자동 static 대체는 없다. `static`은 명시적 무료 정적 분석·정책 계획 모드다. 분석·계획에 사용한 모델 선택은 접수 때 고정한다.

## 입력과 계획 인계

단일 애플리케이션과 현재 AWS ApplicationSet의 한 타깃을 지원한다. 복수 후보는 사용자가 선택한다. unknown/suggested를 검증된 관측값으로 바꾸지 않는다. 필수 포트·환경 바인딩이 없으면 질문하고, runtime/readiness 오류나 미지원 볼륨 등 source 문제는 code_review로 중단한다. 시작 명령이 없으면 이미지/빌더 기본값을 사용하며 실제 실행 실패는 배포 로그로 진단한다.

변수는 `{key,value}`의 공개 값 또는 `{key,secretRef,secretKey}`의 기존 Kubernetes Secret 참조다. 민감한 키의 실제 값은 받지 않는다. Secret은 `svc-{serviceId}` namespace에 미리 준비한다. runtime Secret은 빌드 단계 Secret을 공급할 수 없어 그 필요성이 확인되면 중단한다. 플랫폼 `PORT`는 확인한 포트와 같아야 하고 IRIS 플랫폼 변수는 덮어쓰지 않는다.

분석기 원본 `deploymentDossier`·planning report를 유지한다. 아직 생성하지 않은 이미지·실행 검증·새 인프라 바인딩은 portable dossier에 blocked로 남을 수 있다. WAS는 분석 원문 snapshot/result digest와 확인된 설정/운영 타깃을 묶은 별도 `iris.pipeline-plan.v1`을 만든다. 이 계획의 SHA-256을 큐에 함께 전달하고, Worker가 source/config/targets/digest 동일성을 검사한다. 원본 분석기의 `executionAuthorized:false`는 유지한다. 사용자 파이프라인 요청이 기존 플랫폼 Worker 실행을 허용한다.

소스 head·서비스 build 설정·타깃·설치 접근이 도중에 바뀌면 이전 계획을 접수하지 않는다. BUILD는 고정 SHA를 다시 내려받아 분석기의 snapshot ID와 비교하고, 재현 가능한 archive SHA-256을 기록하여 S3→CodeBuild→ECR digest로 연결한다. Railpack 0.40.1의 검증된 CLI checksum, `prepare` 및 고정 BuildKit frontend를 사용한다.

GitOps는 실제 인프라 계약 `services/{id}/prod/values.yaml` 및 Argo Application `svc-{id}`를 사용한다. FF commit과 release trailer를 기록하고 Synced+Healthy를 관찰한다. 실패 자동 rollback은 이전 정상 manifest와 현재 서비스 subtree가 일치하는 경우에만 수행하며, revert revision이 Synced+Healthy가 될 때까지 기다린다. 수동 과거 커밋 재배포·롤백은 이번 파이프라인의 실행 계약에 포함하지 않는다. 웹은 해당 기존 버튼을 제공하지 않고 새 분석 배포를 안내한다. 관리된 서비스의 기존 raw deployment POST는 `409 PIPELINE_REQUIRED`다. 관리되지 않은 legacy endpoint가 만드는 raw BUILD는 이 Worker에서 실행할 수 없다.

## 실행과 환경

```sh
uv sync --frozen --extra analysis
uv run --extra analysis alembic upgrade head
uv run --extra analysis uvicorn app.main:app --host 0.0.0.0 --port 8000
# 별도 프로세스 / Deployment
uv run --extra analysis python -m app.workers.analysis_worker
uv run --extra analysis python -m app.workers.pipeline_worker
uv run --extra analysis python -m app.workers.build_worker
uv run --extra analysis python -m app.workers.deploy_worker
uv run --extra analysis python -m app.workers.diagnosis_worker
```

`.env.example`은 GitHub App, `ANALYSIS_*`, AWS CodeBuild/artifact bucket, 별도 GitOps App, Argo 읽기 token, `DIAGNOSIS_*` 키를 나열한다. Control API에는 빌드·GitOps 쓰기·진단 모델 권한을 주지 않고 각 Worker에 필요한 IAM/credentials를 제공한다. Docker는 두 고정 Python agent wheel을 포함한다. AI 분석·계획에는 고정 OpenCode 1.18.33 실행파일 또는 같은 정책의 서버가 추가로 필요하다.

분석과 진단의 budget ledger를 영속 공유 경로로 맞춘다. 기본 /tmp는 호스트 내에만 합산되며, 여러 호스트의 전역 비용 제어를 제공하지 않는다. 유료 호출 전 예약하고 모델 시작 상태를 저장한다. 모델 실행 후 프로세스/lease가 유실돼 결과를 알 수 없으면 불확실한 복구 실패로 남겨 중복 유료 실행을 막는다.

진단은 upstream diagnosis-request.v1/result.v2와 OpenAI runtime을 재사용한다. `DIAGNOSIS_MODEL/API_KEY/INPUT_USD_PER_MILLION/OUTPUT_USD_PER_MILLION`을 운영자가 지정한다. CodeBuild 로그에는 `DIAGNOSIS_AWS_REGION/CODEBUILD_PROJECT`와 CloudWatch 읽기 권한이 필요하다. 수집한 실제 CloudWatch/Argo 메시지만 마스킹·제한·출처와 함께 제공한다. 로그가 없으면 만들지 않는다. 진단 실패·미설정·timeout은 원래 배포 실패 상태를 보존하며 remediation은 자동 실행하지 않는다.

환경 바인딩은 iris-infra `iris-service` chart 0.5.0(`environment` 필드) 변경과 함께 배포해야 한다. 해당 차트 merge/tag 및 ApplicationSet 적용 전에 변수 포함 values를 기존 차트에 보내면 schema 검증이 실패한다.

## 검증 범위

실제 PostgreSQL과 실제 두 agent 패키지를 사용하고 GitHub/AWS/GitOps/Argo/모델 외부 실행은 통제된 대역으로 검사한다. 원클릭·질문 재개·고정 소스/설정 변경 차단·빌드/배포 성공·실패 진단·Git/CodeBuild 응답 유실 복구·lease fencing·안전한 rollback을 검증한다. 웹은 TypeScript/tests/build와 브라우저 fixture에서 질문·계획·진단·새로고침 복원을 확인한다. 유료 모델이나 운영 클라우드 배포를 실행한 검증은 아니다.
