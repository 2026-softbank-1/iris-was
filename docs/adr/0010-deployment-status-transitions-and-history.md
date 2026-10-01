# 0010. 배포 요청 상태는 전이 함수로만 바꾸고 전이마다 이력을 남긴다

- 상태: 수락됨
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
배포 요청의 `status` 는 Build Worker·Deploy Worker·API 가 각자 바꾼다. 막는 장치가 없으면 `QUEUED` 에서 곧바로 `SUCCEEDED` 로 건너뛰거나, 끝난 배포가 다시 진행 중으로 돌아간다. 진행 중 요청은 서비스·환경당 하나라는 부분 유니크 인덱스(`uq_deployment_requests_active`)도 상태가 엉키면 깨진다.

또 화면은 "Initializing → Building → Deploying → Active" 흐름과 단계별 소요 시간을 보여줘야 한다. `deployment_requests.updated_at` 은 마지막 변경 시각 하나뿐이라 단계별 시간을 알 수 없다.

## 검토한 선택지
1. 호출하는 쪽이 `status` 를 직접 UPDATE — 구현은 없지만 규칙이 흩어져 어긋난다.
2. 전이 함수 하나 + 이력 테이블 — 규칙이 한곳이고 이력으로 시간을 계산한다. 함수를 거치지 않는 직접 UPDATE 는 막지 못하므로 합의가 필요하다.
3. 상태별 시각 컬럼(`building_at` …)을 `deployment_requests` 에 추가 — 단순하지만 재시도·되돌림 같은 반복 전이를 담지 못한다.

## 결정
2번. `DeploymentStatusService.transition_status(deployment_request_id, to_status, failure_code=None)` 가 `status` 를 바꾸는 유일한 경로다. API 와 Worker 가 같이 쓴다.

- 행을 `SELECT ... FOR UPDATE` 로 잠가 읽고 허용 표를 검사한 뒤 `status` 를 바꾸고 이력을 한 줄 쌓는다. 커밋은 호출하는 쪽이 한다.
- 허용 표

| from | 허용 to |
|---|---|
| `QUEUED` | `BUILDING`, `FAILED` |
| `BUILDING` | `DEPLOYING`, `FAILED` |
| `DEPLOYING` | `SUCCEEDED`, `FAILED`, `ROLLED_BACK`, `MANUAL_INTERVENTION` |
| `FAILED` | `ROLLED_BACK`, `MANUAL_INTERVENTION` |
| `SUCCEEDED` · `ROLLED_BACK` · `MANUAL_INTERVENTION` | (끝) |

- 표에 없는 전이는 `409 INVALID_STATUS_TRANSITION`(`InvalidStatusTransitionError`)이다.
- 이미 그 상태면 아무것도 하지 않는다(예외·이력 없음). jobs 큐가 at-least-once 라 Worker 가 같은 전이를 다시 해도 안전해야 한다.
- 실패는 `FAILED` 하나다. 타임아웃·에러·`CrashLoopBackOff` 도 모두 `FAILED` 이고, 원인은 `failure_code` 로 구분한다. `FAILED` 로 갈 때만 `failure_code` 를 받고 반드시 있어야 한다. 상태 값은 늘리지 않는다.
- 이력 테이블 `deployment_status_histories`: `from_status`(첫 행만 비어 있음), `to_status`, `failure_code`, `created_at`(전이 시각). 쌓기만 하고 고치지 않는다.
- 배포 요청을 만들 때(`DeploymentRequestService.create_deployment_request`) 첫 행 `(없음 → QUEUED)` 을 남긴다. 푸시 웹훅과 수동 배포가 모두 같은 함수를 지나므로 이력이 빠지지 않는다. 이력이 없는 옛 요청은 `created_at` 부터 현재 상태 하나로 본다.
- 단계별 소요 시간은 이력의 시각 차이다. 한 상태에 머문 구간은 다음 전이 시각에 끝나고, 마지막 구간은 열려 있다.
- 화면 용어와 DB 상태: Initializing = `QUEUED`, Active = `SUCCEEDED`.

수동 배포 API(`POST /api/v1/services/{id}/deployments`)는 ADR 0009 가 예고한 대로 같은 `DeploymentRequestService` 를 쓴다. `trigger_type` 은 `MANUAL`·`REDEPLOY`·`ROLLBACK` 이다.

- `MANUAL`: 서버가 서비스 브랜치의 최신 커밋을 GitHub 에서 읽는다(`sourceSha` 로 덮어쓸 수 있다).
- `REDEPLOY`·`ROLLBACK`: 같은 서비스의 이전 배포(`sourceDeploymentId`)의 커밋을 복사한다. `ROLLBACK` 의 원본은 `SUCCEEDED` 여야 한다. 이 API 는 그 커밋으로 새 빌드·배포를 요청할 뿐이고, GitOps revert commit 은 Deploy Worker 몫이다.
- 멱등성 키는 `manual:{service_id}:{Idempotency-Key 헤더 또는 uuid}` 다. 같은 헤더로 다시 보내면 처음 만든 요청을 돌려주고, 키가 없는데 진행 중인 배포가 있으면 `409 DEPLOYMENT_IN_PROGRESS` 다.

## 결과
- 상태 규칙이 한곳에 모이고 상태 흐름이 이력으로 남는다. 배포 목록·상세 API 가 이력과 단계별 소요 시간을 준다.
- 함수를 거치지 않는 직접 UPDATE 는 막지 못한다. Worker 는 `status` 를 직접 쓰지 않고 이 함수를 부르기로 합의했다.
- 이 변경은 `deployment_requests` 만 다룬다. 타깃별 `releases.status` 전이는 다루지 않는다. 필요해지면 새 ADR 로 같은 방식을 확장한다.
- `main` 의 Build Worker 는 `develop` 과 스키마가 달라 이 함수를 아직 쓰지 않는다. 두 갈래를 합칠 때 `Build` 의 상태 변경을 이 함수로 바꾸고, `INITIALIZING`·`SUPERSEDED` 가 상태에 들어오면 허용 표를 넓힌다.
