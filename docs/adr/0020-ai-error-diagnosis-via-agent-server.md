# 0020. 실패한 배포의 AI 진단은 Control API 가 에이전트 서버를 호출하고 결과를 DB 에 저장한다

- 상태: 채택됨 (운영 반영·검증 완료. 시작 방식은 "실패 확정 뒤 자동 시작"으로 바뀌었다)
- 날짜: 2026-10-03
- 결정자: 김지민

## 배경
Notion task "[API] AI 에러 진단 연동"의 완료 기준은 "실패 배포에서 'AI 진단' → 원인·해결책 표시"다. 합의(Slack 10/2, 김환 ↔ 김현겸)는 WAS 가 배포의 로그·배포 정보·소스 정보를 JSON 으로 묶어 에이전트의 `/diagnose` 에 `data` 로 넘기는 것이다.

task 에는 에이전트 호출 방식(URL·메서드·인증, 응답 형식, 동기/비동기)이 "미정"으로 남아 있었다. 에이전트 레포(`iris-error-check-agent` 의 README, `docs/BACKEND_JSON_V04.md`, `src/ai_error_check_agent/api.py`)에서 확인했다.

- `POST /diagnose`, 인증은 `X-API-Key` 헤더, 본문은 `success/message/data` 전체 또는 `data` 만 받는다.
- 응답은 봉투 없이 `diagnosis-result.v3` 다. 원인은 `analysis.hypotheses`, 해결책은 `analysis.remediation.plans` 이고 근거 로그는 `evidence` 다. 동기로 답한다.
- 소스는 HTTPS S3 presigned URL 의 `.tar.gz` 로 받고, 필요할 때만 내려받는다. 소스를 못 읽어도 로그 진단은 유지한다(HTTP 200).
- 마스킹한 로그+메타데이터가 16KiB 를 넘으면 거절(`422 INPUT_TOO_LARGE`)하고, 동시 처리는 2건이다(`429 BUSY`). 모델 호출은 최대 2번이고 호출마다 60초 제한이다.
- 에이전트 README 는 "배포 소유권과 호출 권한 확인, 비동기 작업 DB·Worker 는 후속 구현"이라고 적는다. 이 서버가 그 역할이다.

같은 시기에 `iris-was` PR #9(`feat/ai-analysis-integration`, 초안)가 분석·진단 전체를 PipelineRun 과 Worker 다섯 개로 묶는 설계를 올렸다. 팀은 Slack(10/2)에서 "구현된 것은 그대로 두고 WAS 는 단순히 넘기는 역할만 한다"로 정리했다.

## 검토한 선택지
1. Control API 가 에이전트를 부른다 — 에이전트 API 가 이미 동기라 단순하고, 새 컴포넌트가 없다. 호출이 최대 2분 남짓 걸린다. 이를 (1a) 요청 안에서 기다리거나 (1b) 응답을 먼저 보내고 서버 프로세스 안에서 이어 가며 화면이 폴링하게 할 수 있다.
2. `DIAGNOSE` job 으로 큐에 넣고 Worker 가 호출한다 — 서버가 죽어도 이어받고 화면이 폴링만 하면 된다. 하지만 job 종류 추가(마이그레이션), 새 Worker 와 그 Worker 의 Loki·S3·에이전트 권한이 필요하고, jobs 큐가 at-least-once 라 유료 모델 호출이 중복될 수 있어 "호출 직후 결과를 먼저 기록" 같은 별도 처리가 더 필요하다. PR #9 의 방향이고 합의한 범위("단순 전달")보다 크다.
3. 프런트가 에이전트를 직접 호출한다 — 에이전트 API 키를 프런트에 둘 수 없고(에이전트 문서도 키를 프런트 번들에 넣지 말라고 한다), 배포 소유권을 확인할 수 없다.

## 결정
1 을 택하고 (1b) 로 한다. 처음에는 (1a) 로 만들었지만 iris-infra 를 확인해 바꿨다: 공유 ALB 에 idle timeout 설정이 없어(`load-balancer-attributes` 에 access log 만 있다) 기본 60초가 적용된다. 모델 호출이 60초를 넘으면 브라우저는 `504` 를 받고 서버만 끝까지 돌게 된다.

- `POST /services/{id}/deployments/{deploymentId}/diagnose` 는 진행 중(`RUNNING`) 행을 만들어 커밋하고 **바로 `202`** 로 답한다. 에이전트 호출은 응답을 보낸 뒤 같은 프로세스의 백그라운드 작업(`BackgroundTasks`)이 새 DB 세션으로 이어서 하고, 화면은 `GET .../diagnosis` 를 `SUCCEEDED`·`FAILED` 가 될 때까지 폴링한다. `FAILED`·`ROLLED_BACK`·`MANUAL_INTERVENTION` 배포만 진단한다(아니면 `409 DEPLOYMENT_NOT_FAILED`).
- 진단은 `deployment_diagnoses` 에 한 행으로 남긴다(`RUNNING → SUCCEEDED·FAILED`). 에이전트를 부르기 전에 `RUNNING` 행을 먼저 커밋하고, 배포 요청마다 `RUNNING` 은 하나만 둘 수 있게 부분 unique index 를 건다(`409 DIAGNOSIS_IN_PROGRESS`). 모델 비용이 겹쳐 나가지 않게 하려는 것이다. 백그라운드 작업은 프로세스 재시작(배포 롤아웃 등)에 사라질 수 있으므로, 서버가 죽어 남은 `RUNNING` 은 4분 뒤 `DIAGNOSIS_ABANDONED` 로 닫고 다시 진단할 수 있다. 읽기 트랜잭션도 외부 호출 전에 닫아 DB 연결을 2분 동안 잡지 않는다.
- 성공한 진단이 있으면 모델을 다시 부르지 않고 `200` 으로 그 결과를 돌려준다. `refresh=true` 는 새로 진단하고(`202`) 이력을 쌓는다. 실패한 행은 캐시로 쓰지 않는다.
- 에이전트 응답 전체를 `result`(JSONB)에 저장하고, API 응답은 이 서버가 읽는 필드(`analysis`·`evidence`·`sourceAnalysis`·`inputLimitations`)만 camelCase 로 내보낸다. 쓸 수 없는 응답(`job_status` 가 성공이 아니거나 `analysis` 가 없음)은 성공으로 저장하지 않고 행을 `FAILED`(`error_code`)로 닫는다. 응답은 이미 `202` 로 나갔으므로 실패는 폴링에서 `status=FAILED` 로 알린다. 에이전트의 `error.code` 는 `error_code` 에 남기고 message 는 입력 일부를 담을 수 있어 옮기지 않는다.
- 입력은 다음과 같이 만든다.
  - **로그**: Loki 에서 `svc-{service_id}` 의 런타임 로그를 읽는다(요청 생성 시각부터 마지막 상태 변경 5분 뒤까지, 최신 1000건). 에이전트 입력 한도에 맞춰 가장 최근 줄부터 예산(12KB, 거절당하면 6KB)만큼 고르고, 잘렸으면 `logRange.isComplete=false` 로 알린다. 로그가 하나도 없으면 에이전트를 부르지 않고 행을 `DIAGNOSIS_LOGS_UNAVAILABLE` 로 닫는다. 없는 로그를 만들어 내지 않는다.
  - **실패 단계**: `failure_code` 로 가린다. `BUILD_*`·`SOURCE_*` 는 `build`, `DEPLOY_FAILED`(Sync·readiness 실패, 앱이 못 뜬 경우가 많다)는 `runtime`, `DEPLOY_TIMED_OUT`·`DEPLOY_INFRA_ERROR` 는 `deploy` 다. 종료 코드는 모르므로 보내지 않는다.
  - **빌드 로그**: `build` 단계로 실패했으면(`BUILD_*`·`SOURCE_*`) Loki 대신 Build Worker 가 `builds.log_tail` 에 남긴 빌드 로그를 쓴다(아래 "빌드 로그 수집"). 로그는 `stage=build`·`sourceId=codebuild`·`stream=combined` 로 보낸다.
  - **소스**: 빌드가 S3 에 올린 스냅샷(`snapshots/{build_id}.tar.gz`)의 presigned URL, 커밋 SHA(40자리일 때만), `rootDirectory` 를 보낸다. 롤백·재시작처럼 빌드하지 않은 요청은 원본 요청의 빌드를 따라간다. 스냅샷은 하루 뒤 지워지므로 23시간이 지난 빌드는 소스를 보내지 않는다. URL 은 로그·DB 에 남기지 않는다.
  - 환경변수 값(`variables_snapshot`)은 보내지 않는다.
- 진단 결과로 배포 요청의 상태를 바꾸지 않는다. 해결책은 제안일 뿐 서버가 실행하지 않는다.
- 설정은 `DIAGNOSIS_AGENT_URL`·`DIAGNOSIS_AGENT_API_KEY`(없으면 진단 실행이 `503 NOT_CONFIGURED`, 저장된 진단 조회는 가능)와 선택인 `AWS_REGION`·`ARTIFACT_BUCKET`(둘 다 있어야 소스를 보낸다)이다.

## 결과
- (시작은 아래 "실패 확정 뒤 자동 시작"에서 서버가 한다. POST 는 다시 시도·다시 진단·옛 배포용이다.) 프런트는 GET 을 폴링해 원인·해결책을 받는다(최대 150초, 에이전트 대기 한도). ALB idle timeout 과 무관하다. 폴링 중 `RUNNING` 이 4분을 넘으면 서버가 죽은 것이니 다시 시작하면 된다. 재시작에도 이어 가야 한다고 판단되면 `DIAGNOSE` job 으로 옮긴다. 진단 행(`RUNNING → 결과`)과 `GET` 은 그대로 쓸 수 있다.
- **Control API 에 S3 읽기 권한이 생긴다.** "CodeBuild·Git·Argo CD 는 Worker 만 호출하고 Control API 는 AWS 권한이 없다"는 원칙의 예외다. `presign` 은 로컬 서명이라 호출은 없지만, 받는 쪽이 읽으려면 Control API 의 IAM Role 에 스냅샷 버킷 `snapshots/*` 의 `s3:GetObject` 만 허용해야 한다(iris-infra 작업). 권한을 주기 전에는 설정을 비워 로그만 진단한다.
- **빌드 단계 실패의 진단은 Build Worker 가 로그를 남겨야 한다.** 아래 "빌드 로그 수집"으로 해결했지만 Build Worker 역할에 CloudWatch Logs 읽기 권한이 있어야 동작한다. 앱이 뜨기 전에 실패한 배포(`ImagePullBackOff` 등)는 앱 로그가 없어 진단하지 못하며, 쿠버네티스 이벤트 수집이 필요하다.
- 에이전트는 동시에 2건만 처리한다. 넘으면 `429` 를 받아 진단이 `FAILED`(`BUSY`)로 끝나고, 사용자가 다시 누르면 된다.
- 마이그레이션 `2dc16dd598ea` 가 `deployment_diagnoses` 를 추가한다.

## 운영 연결 확인 (2026-10-03, iris-infra·iris-gitops-environments 기준)
에이전트 주소 `http://iris-platform-error-agent.iris-platform.svc.cluster.local:8001` 를 인프라 정의로 확인했다.

- Helm release `iris-platform`, Namespace `iris-platform`, Service `{release}-error-agent`(ClusterIP, 포트 8001)와 일치한다.
- Deployment 는 `errorAgent.enabled`(켜져 있음)와 이미지 digest 가 모두 있어야 렌더된다. digest 는 `iris-gitops-environments/platform/aws-dev-management/error-check-agent.yaml` 에 2026-10-02 커밋돼 있고, 그 이미지가 ECR 에 있다.
- NetworkPolicy `application-boundary` 가 같은 Namespace 파드 사이의 ingress·egress 를 열어 두어 API 파드에서 에이전트(8001)로 갈 수 있다. 추가 규칙은 필요 없다.
- 이 주소는 에러 진단 에이전트다(`/healthz`·`/models`·`/diagnose` 만 있다). 코드 분석기는 운영 서버가 없다(runbook: "Code Analyzer는 후속 작업").
- 운영에 넣을 값: Secret `iris-platform-was-env` 에 `DIAGNOSIS_AGENT_URL`(위 주소)과 `DIAGNOSIS_AGENT_API_KEY`(Secret `iris-error-agent` 의 `AGENT_API_KEY` 와 같은 값). 현재 배포된 WAS 이미지는 이 기능을 포함하지 않는다.
- 클러스터에 접근할 수 없어 실제 파드 상태·Secret 존재·DNS 해석은 확인하지 못했다.

## 빌드 로그 수집 (2026-10-03 추가)
빌드 단계 실패가 가장 흔한 실패인데 CodeBuild 로그는 CloudWatch 에만 있어(`/aws/codebuild/<프로젝트>`, 30일 보관) DB 에는 링크(`builds.log_url`)뿐이었다. Control API 에 CloudWatch 권한을 더 주는 대신, 이미 CodeBuild 를 다루는 Build Worker 가 실패를 확정할 때 로그 끝부분을 남긴다.

- Build Worker 는 빌드가 `FAILED`(사용자 소스·설정 단계)나 `TIMED_OUT` 으로 끝나면 CloudWatch 에서 마지막 300줄을 읽어 `builds.log_tail`(JSONB, `{"entries": [{"timestamp", "message"}], "is_truncated"}`)에 저장한다. 인프라 오류로 재시도할 때는 읽지 않는다.
- 저장 전에 소스 스냅샷 presigned URL 의 서명, `Authorization` 헤더, GitHub 토큰 모양을 `[REDACTED]` 로 가리고, 한 줄 2,000자·200줄·64KB 로 줄인다(최근 줄을 남긴다). 진단 에이전트도 마스킹하지만 DB 에는 가린 값만 둔다.
- **읽기에 실패해도 빌드 결과는 바뀌지 않는다.** 권한이 없거나 로그가 아직 없으면 경고 로그만 남기고 `log_tail` 을 비운다. 이 경우 진단은 `DIAGNOSIS_LOGS_UNAVAILABLE` 로 끝난다.
- 진단은 `failure_code` 가 `BUILD_*`·`SOURCE_*` 이면 이 로그를 쓰고(런타임 로그·Loki 는 보지 않는다), 로그 범위는 첫 줄~마지막 줄 시각이며 `is_truncated` 면 `isComplete=false` 로 알린다.
- **마지막 실패 표시줄(`Phase complete: BUILD State: FAILED`) 뒤는 버리고 보낸다.** 운영 E2E(2026-10-03)에서 찾았다: CodeBuild 는 단계가 실패해도 POST_BUILD·UPLOAD_ARTIFACTS 를 이어서 돌리고 실패한 스크립트를 통째로 다시 출력해, 최근 줄부터 고르는 에이전트 입력 예산(12KB)이 이 잡음에 소진돼 실제 오류 출력(실패 표시줄 앞 18줄)이 밀려났고 진단이 `insufficient_evidence` 로 끝났다. 읽는 시점에 자르므로 이미 저장된 로그에도 적용되고, 표시줄이 없으면(시간 초과 등) 그대로 둔다.
- **iris-infra 에 필요한 권한**: Build Worker 역할(`iris-dev-build-worker`, `terraform/environments/aws/dev/foundation/build.tf` 의 `aws_iam_role_policy.build_worker`)에 아래를 더한다. 로그 그룹 ARN 에 `:*` 를 붙이면 로그 스트림까지 덮는다.

```hcl
{
  Sid      = "ReadBuildLogs"
  Effect   = "Allow"
  Action   = ["logs:GetLogEvents"]
  Resource = "${aws_cloudwatch_log_group.build.arn}:*"
}
```

- 이 권한이 적용되기 전에 끝난 빌드와, CodeBuild 를 시작하기 전에 실패한 요청(`SOURCE_*`: 소스 접근·커밋 없음·용량 초과), 인프라 오류로 재시도를 소진한 요청(`BUILD_INFRA_ERROR`)은 로그가 없어 진단하지 못한다.
- 마이그레이션 `4682081516de` 가 `builds.log_tail` 을 추가한다.

## 실패 확정 뒤 자동 시작 (2026-10-03 추가, 결정 변경)
처음에는 사용자가 "AI 진단" 버튼을 눌러야 진단이 시작됐다(`POST .../diagnose` 가 유일한 시작점). 사용자가 "배포 중 에러가 나면 따로 트리거 없이 진단이 돌아야 한다"고 정해, 실패가 확정되면 서버가 진단을 시작한다.

- **결정**: Control API 안에서 주기적으로(기본 5초) "실패가 확정됐는데 진단 기록이 없는 배포"를 찾아 시작한다(`AutoDiagnosisRunner`, lifespan 에서 켠다). 대상은 `FAILED`·`ROLLED_BACK`·`MANUAL_INTERVENTION` 이고, `REMOVE` 요청이 아니며(떠 있는 앱을 내리는 요청이라 진단할 로그가 없다), 서비스·프로젝트가 삭제되지 않았고, 마지막 상태 변경이 10분 안(`AUTO_MAX_AGE`)인 요청이다. 진단 행이 이미 있으면(성공·실패·진행 중) 다시 시작하지 않는다. 자동 시작은 배포당 한 번이고, 실패하면 사용자가 "다시 시도"한다. 서버가 죽어 4분 넘게 `RUNNING` 으로 남은 행만 10분 안이면 다시 시작한다.
- **왜 주기적으로 찾는가**
  - Worker 가 상태를 바꿀 때 시작하는 방식은 안 된다. 상태는 Worker 프로세스가 바꾸지만 에이전트·Loki 설정과 호출은 Control API 에만 있다. 옮기면 Worker 가 에이전트 호출·Loki 읽기를 하게 되어 선택지 2 에서 피한 문제가 다시 생긴다(컴포넌트끼리 Secret·권한을 나누지 않는다).
  - `DIAGNOSE` job 으로 큐에 넣는 방식은 선택지 2 와 같은 이유로 미룬다(job 종류·Worker·권한, at-least-once 로 유료 호출이 중복될 수 있다).
  - `GET` 이 읽을 때 시작하는 방식은 보지 않으면 진단이 안 돌고 읽기에 부작용이 생긴다.
  - 주기적 탐색은 마이그레이션이 없고(`requested_by` 는 원래 비어 있을 수 있다), 재시작·교체 중 놓친 실패도 다음 주기에 잡으며, 여러 Pod 가 돌아도 `RUNNING` 부분 unique index 가 중복 진단을 막는다.
- **요청자**: 자동으로 시작한 진단은 `requested_by` 가 비어 있다. 로그 조회·진단 실행의 소유권 확인은 서비스를 가진 프로젝트의 소유자 기준으로 한다.
- **비용·동시성 보호**: 한 번에 하나만 돌린다(`AUTO_MAX_RUNNING=1`). 사용자가 시작한 진단이 돌고 있어도 기다리므로 에이전트의 동시 2건 중 한 자리는 항상 버튼용으로 남는다. 가장 오래 기다린 실패부터 처리한다. 10분 창이라 이 기능을 켠 직후 옛 실패를 한꺼번에 되살리지 않는다(백필 없음). `DIAGNOSIS_AUTO_START_ENABLED=false` 로 끌 수 있고(끄면 버튼으로 시작하는 진단만 남는다), 에이전트 설정이 없으면 켜지 않는다.
- **API 계약은 그대로다.** `POST`(다시 시도·`refresh=true`·창을 넘긴 옛 배포)와 `GET` 은 바뀌지 않는다. 자동 진단이 도는 중에 `POST` 하면 `409 DIAGNOSIS_IN_PROGRESS`, 성공한 진단이 있으면 `200` 이다. 달라지는 것은 실패가 확정된 뒤 탐색 주기(5초) 안에 `RUNNING` 행이 생긴다는 점이고, 그 전에는 `GET` 이 `404 DIAGNOSIS_NOT_FOUND` 다.
- **종료**: 종료 신호를 받으면 진행 중인 진단을 최대 20초 기다리고 넘으면 취소한다. 남은 `RUNNING` 행은 다른 Pod 가 4분 뒤 낡은 행으로 보고 다시 시작한다.
- **한계**: 실패마다 모델 비용이 든다(빌드 단계 실패는 로그가 없으면 에이전트를 부르지 않고 `DIAGNOSIS_LOGS_UNAVAILABLE` 로 닫는다). 10분 넘은 옛 실패는 자동으로 진단하지 않는다(버튼의 `POST` 로는 된다). 상태 전이를 알림(LISTEN/NOTIFY)으로 받지 않고 DB 를 주기적으로 읽는다. 배포 수가 크게 늘면 알림으로 바꾼다.
