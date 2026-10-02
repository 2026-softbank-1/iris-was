# 0015. 롤백과 재시작은 이미 빌드한 이미지를 다시 배포하는 요청으로 만든다

- 상태: 수락됨
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
Notion task "[API] 배포 액션 API (Restart / Redeploy / Rollback / Remove)"의 완료 기준은 "깨진 배포 후 원클릭 롤백으로 이전 버전 복구"(데모 3단계)다. 수동 배포 API(ADR 0010)에는 이미 `REDEPLOY`·`ROLLBACK` 이 있었지만, 둘 다 원본의 `source_sha` 만 복사해서 BUILD job 부터 다시 시작했다. 그래서 롤백이 다시 빌드하는 시간만큼 느리고, 소스가 같아도 빌드 결과가 달라질 수 있었다(의존성 갱신·네트워크 실패).

Deploy Worker 는 이미 이 요구를 받을 준비가 돼 있다. release 의 기준은 image digest 이고, `values.yaml` 의 `release.id` 가 Pod annotation 으로 들어가 digest 가 같아도 release 마다 rollout 된다. 필요한 것은 BUILD 를 건너뛰고 기존 이미지로 DEPLOY job 을 만드는 경로뿐이다.

## 검토한 선택지
1. 롤백·재시작도 `REDEPLOY` 처럼 커밋을 복사해 다시 빌드한다 — 코드는 그대로지만 느리고 결과가 같다는 보장이 없다.
2. Deploy Worker 가 DEPLOY job 에서 "원본 release 의 digest 를 쓴다"는 분기를 갖는다 — Worker 가 `builds` 를 읽는 방식(`payload.build_id`)이 깨진다.
3. 원본 요청의 성공한 빌드를 복사해 새 요청에 붙이고 DEPLOY job 으로 시작한다 — Worker 는 바뀌지 않는다. 요청마다 빌드 한 건이라는 모델(ADR 0004)이 유지된다.

## 결정
3 을 택한다.

- `trigger_type` 에 `RESTART` 를 추가한다. `ROLLBACK`·`RESTART` 는 소스를 빌드하지 않고, `REDEPLOY` 는 기존대로 같은 커밋을 다시 빌드한다.
  - `ROLLBACK`: 사용자가 `sourceDeploymentId` 로 고른 `SUCCEEDED` 배포의 이미지.
  - `RESTART`: 서비스에서 마지막으로 `SUCCEEDED` 가 된 배포(지금 떠 있는 버전)의 이미지. 원본을 보내지 않는다. 성공한 배포가 없으면 `409 NO_SUCCEEDED_DEPLOYMENT`.
- 새 요청은 원본의 `source_sha`·`source_commit_message`·`variables_snapshot` 을 가져오고, 원본 요청을 `deployment_requests.source_deployment_request_id` 로 가리킨다(재배포도 같이 기록한다). Notion 스펙의 `rollback_of_id` 는 이 컬럼이다. 롤백만이 아니라 세 동작이 같은 계보를 쓰도록 이름을 넓혔다.
- `DeploymentRequestService.create_deployment_request_reusing_image` 가 한 트랜잭션에서 요청, 원본 빌드를 복사한 성공 상태의 `builds` 행(`Build.copy_succeeded`), DEPLOY job 을 만들고 요청을 `DEPLOYING` 으로 옮긴다. 원본 빌드가 성공하지 않았거나 digest 가 없으면 `422 INVALID_INPUT` 이다.
- 상태 전이 표에 `QUEUED → DEPLOYING` 을 추가한다. 이 요청은 `BUILDING` 을 거치지 않으므로 이력은 `(없음 → QUEUED)`, `(QUEUED → DEPLOYING)` 두 줄이고 단계별 소요 시간에 빌드 구간이 없다. ADR 0010 의 "진행 중 상태끼리는 앞으로만 움직인다"는 지켜진다.
- 같은 서비스에 진행 중인 요청이 있으면 `409 DEPLOYMENT_IN_PROGRESS` 다. 멱등성 키 규칙도 ADR 0010 과 같다.
- 롤백·재시작 배포가 실패하면 기존 자동 revert(job `ROLLBACK`)가 `previous_good_release_id` 로 되돌린다. 재시작의 이전 정상 release 는 같은 이미지라 되돌아가도 서비스는 그대로다.

## 결과
- 롤백이 빌드 없이 GitOps 커밋 한 번과 Argo 동기화로 끝난다. Deploy Worker 코드는 바뀌지 않았고, 통합 테스트(`tests/test_deploy_flow.py`)가 같은 digest 의 새 release 로 배포되는 것까지 확인한다.
- 롤백 대상 이미지가 ECR 에 남아 있어야 한다. 성공한 release 의 이미지는 Worker 가 `r-{release_id}` 태그로 지키므로 `SUCCEEDED` 배포는 유지된다. 이미지가 사라진 경우는 API 가 알 수 없고, rollout 이 실패해 자동 revert 로 이어진다.
- 마이그레이션 `dee7264e421e` 가 컬럼·외래 키·`deployment_trigger` CHECK 를 바꾼다. RESTART 요청이 남아 있으면 downgrade 가 실패한다(배포 이력이라 지우지 않는다).

## 미룬 것: Remove
이 task 의 Remove(서비스를 클러스터에서 내리는 동작)는 이 ADR 에서 구현하지 않았다. 이유는 클러스터 쪽 결정이 먼저 필요해서다. 이후 [ADR 0016](0016-remove-service-deployment.md) 에서 정책을 정하고 구현했다.

- iris-infra 의 사용자 서비스 ApplicationSet(`helm/gitops/templates/services.yaml`)이 `applicationsSync: create-update` 이고 주석이 "A removed directory must not delete a running service; deletion is a separate decision" 이라고 적는다. 그래서 Deploy Worker 가 `services/{id}/prod` 를 GitOps 에서 지워도 Application 과 Pod 는 남는다.
- 삭제를 클러스터에 반영하려면 인프라가 정책을 `sync` 로 바꾸거나(디렉터리 삭제 = 서비스 삭제), 별도 정지 방식(예: `replicas: 0`)을 chart 에 정해야 한다. 이는 "[배포] Restart / Redeploy / Rollback / Remove 실행" task 의 범위다.
- 정해지면 WAS 쪽은 REMOVE job 이 디렉터리를 지우는 커밋(Git tree 에서 항목 삭제)을 만들고 Application 이 사라질 때까지 기다리는 작업이다. `DELETE /services/{id}`(소프트 삭제, ADR 0005)와의 관계도 그때 정한다.
