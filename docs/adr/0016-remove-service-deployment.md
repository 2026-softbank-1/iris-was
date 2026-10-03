# 0016. 서비스 삭제는 GitOps 디렉터리를 지워 ApplicationSet 이 Application 을 정리하게 한다

- 상태: 수락됨 (인프라 적용 대기)
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
Notion task "[API] 배포 액션 API (Restart / Redeploy / Rollback / Remove)"의 Remove 는 돌아가는 서비스를 클러스터에서 내리는 동작("uninstall + 리소스 정리")이다. 지금 `DELETE /services/{id}` 는 DB 에서 삭제 표시만 해서(ADR 0005) 서비스를 지워도 클러스터의 앱은 계속 돈다.

[ADR 0015](0015-rollback-and-restart-reuse-built-image.md) 를 쓸 때 Remove 를 미뤘다. iris-infra 의 사용자 서비스 ApplicationSet(`helm/gitops/templates/services.yaml`)이 `applicationsSync: create-update` 이고 "A removed directory must not delete a running service; deletion is a separate decision" 이라고 적어, GitOps 디렉터리를 지워도 Application 과 Pod 가 남기 때문이다.

## 검토한 선택지
1. **A. 디렉터리 삭제 = 서비스 삭제.** Deploy Worker 가 `services/{id}/prod` 를 지우는 커밋을 올리고, ApplicationSet 이 `applicationsSync: sync` 로 사라진 디렉터리의 Application 을 지운다. 구조가 단순하고 GitOps 가 desired state 라는 원칙과 맞다. 인프라 정책을 바꿔야 하고, 실수로 디렉터리가 지워지면 서비스가 내려간다.
2. **B. 정지(`replicas: 0`).** chart 에 정지 값을 두고 values 만 바꾼다. 안전하지만 Application·Namespace·ALB 규칙이 남아 "리소스 정리"가 안 된다.

## 결정
A 를 택한다(사용자 결정).

- `trigger_type` 과 `job_kind` 에 `REMOVE` 를 추가한다. 사용자는 기존 `POST /services/{id}/deployments` 에 `{"triggerType": "REMOVE"}` 를 보낸다. 롤백·재시작과 같은 배포 요청이라 이력 화면에 같이 남는다.
- 대상은 지금 떠 있는(마지막으로 `SUCCEEDED` 인) 배포다. 성공한 배포가 없거나 마지막 성공이 `REMOVE` 면(이미 내려감) `409 NO_SUCCEEDED_DEPLOYMENT`. 재시작도 내려간 서비스에는 같은 응답이다. 요청에 `sourceSha`·`sourceDeploymentId` 를 보내지 않는다.
- 요청은 빌드·release 를 만들지 않는다. `source_deployment_request_id` 는 내리는 배포를 가리키고 요청은 `QUEUED → DEPLOYING` 으로 곧바로 간다.
- Deploy Worker 의 REMOVE job:
  1. `services/{id}/prod` 를 지우는 커밋을 main 에 fast-forward 한다(`GitHubClient.create_delete_commit`). 커밋 SHA 를 `jobs.external_id` 에 먼저 기록하므로 Worker 가 죽어도 중복 커밋이 없다. 디렉터리가 이미 없으면 커밋하지 않는다.
  2. Argo CD Application `svc-{id}` 가 사라질 때까지 snooze 로 기다린다(시도 횟수를 쓰지 않는다). 사라지면 요청을 `SUCCEEDED` 로 끝낸다.
  3. 10분 안에 사라지지 않거나 커밋 뒤 재시도를 소진하면 `MANUAL_INTERVENTION`(GitOps 는 바뀌었는데 서비스 상태를 알 수 없다). 커밋 전에 재시도를 소진하면 서비스가 그대로라 `FAILED`(`DEPLOY_INFRA_ERROR`).
- 내려간 서비스에는 정상 release 가 없다. 성공한 `REMOVE` 요청보다 앞선 release 는 lastKnownGood 에서, 프로젝트의 online 서비스 수에서, 도메인 `isConnected` 에서 뺀다(`removed_after_release`). release 행은 고치지 않는다. 그래서 내려간 뒤의 새 배포가 실패해도 삭제 전 앱으로 되돌아가지 않는다.
- 내려간 서비스는 `MANUAL`·`REDEPLOY`·`ROLLBACK`(성공했던 이전 배포 지정)으로 다시 올릴 수 있다. 서비스 정의는 남는다.
- GitHub 로 확인한 동작: Git Trees API 에서 항목 `{"path": "services/1/prod", "mode": "040000", "type": "tree", "sha": null}` 은 디렉터리를 지우고, 그 뒤 비는 부모 디렉터리(`services/1`)도 GitHub 이 정리한다. 없는 경로를 지우면 422(`GitRPC::BadObjectState`)라 `NotFoundError` 가 되므로 먼저 `find_subtree_sha` 로 확인한다.

## 인프라 선행 조건
이 구현은 iris-infra 의 ApplicationSet 이 아래처럼 바뀌어야 끝까지 동작한다. 바뀌기 전에 REMOVE 를 요청하면 GitOps 디렉터리만 지워지고 Application 이 남아 10분 뒤 요청이 `MANUAL_INTERVENTION` 으로 끝난다(앱은 계속 돈다. 다시 배포하면 디렉터리가 다시 생긴다).

- `syncPolicy.applicationsSync: sync` — 사라진 디렉터리의 Application 을 지운다.
- Application 템플릿 `metadata.finalizers: [resources-finalizer.argocd.argoproj.io]` — Application 을 지울 때 Pod·Service·Ingress 가 함께 지워지게 한다. 없으면 Application 만 사라지고 리소스가 고아로 남는다.

이 변경은 iris-infra PR 로 올렸다. ApplicationSet 은 root Application 이 고정한 revision 으로 읽으므로, 병합 뒤 운영자가 새 SHA 로 bootstrap 해야 반영된다(iris-infra runbook).

## 결과
- 서비스를 클러스터에서 내리는 경로가 생긴다. 위 인프라 선행 조건이 적용되기 전까지는 REMOVE 를 운영에서 쓰지 않는다.
- 실수로 GitOps `services/` 를 사람이 지우면 서비스가 내려간다. GitOps README 규칙(사람은 `services/` 를 직접 수정하지 않는다)이 더 중요해진다.
- 성공한 REMOVE 요청이 마지막 성공이므로 서비스 응답의 `latestDeployment` 는 `REMOVE`·`SUCCEEDED` 다. 화면이 "내려감"으로 표시하려면 `triggerType` 으로 구분한다.
- 후속: `DELETE /services/{id}`(소프트 삭제)가 REMOVE 를 함께 요청할지는 정하지 않았다(ADR 0005 의 "리소스 정리는 미룬다"). 지금은 서비스를 지워도 앱이 남으므로, 지우기 전에 REMOVE 를 먼저 요청해야 한다.
- 네임스페이스 `svc-{id}` 는 Argo 의 `CreateNamespace` 로 만들어져 Application 삭제 때 남을 수 있다. 정리 정책은 인프라 몫이다.
