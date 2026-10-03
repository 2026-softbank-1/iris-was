# 0022. 서비스·프로젝트를 지우면 떠 있는 앱도 함께 내린다

- 상태: 수락됨 (운영 적용·검증 완료)
- 날짜: 2026-10-03
- 결정자: 김지민

## 배경
`DELETE /services/{id}` 와 `DELETE /projects/{id}` 는 DB 에서 삭제 표시만 했다([ADR 0005](0005-soft-delete-and-deferred-cleanup.md)). 그래서 화면에서 서비스를 지워도 클러스터의 앱은 계속 돌았다. 리소스 정리는 "엔진 작업"으로 미뤄 두었는데, `REMOVE` 배포 요청([ADR 0016](0016-remove-service-deployment.md))이 운영에서 동작하게 되어 이제 연결할 수 있다.

사용자 결정: 삭제하면 DB 삭제도 하고 클러스터에서도 내린다. 별도의 "Remove" 버튼은 만들지 않는다.

## 검토한 선택지
1. **웹이 먼저 `REMOVE` 를 요청하고 끝날 때까지 기다린 뒤 `DELETE` 를 보낸다.** 서버 변경이 없다. 하지만 다른 클라이언트(CLI 등)는 앱을 내리지 못하고, 웹이 2분 넘게 기다려야 한다.
2. **`DELETE` 가 서버에서 소프트 삭제와 `REMOVE` 요청을 한 트랜잭션으로 한다(선택).** 클라이언트가 같은 동작을 얻고, 삭제는 곧바로 끝난다. 앱이 내려가는 것은 Worker 가 이어서 한다.
3. **`REMOVE` 가 성공한 뒤에야 Worker 가 서비스를 삭제 표시한다.** 실패가 화면에 남는다. 삭제가 비동기(2분)가 되고 `DELETE` 응답·목록 동작이 바뀐다.

## 결정
2 를 택한다.

- `ServiceTeardownService.request_teardown` 이 서비스마다 `REMOVE` 요청을 만든다. `DELETE /services/{id}` 와 `DELETE /projects/{id}`(소속 서비스마다)가 소프트 삭제 직전에 부르고, 같은 트랜잭션에서 커밋한다. 응답은 그대로 `204` 다.
- 배포한 적이 있는 서비스에만 요청한다. 가장 최근 요청이 성공한 `REMOVE` 면(이미 내려감) 건너뛴다. 가장 최근이 실패한 첫 배포여도 요청한다. GitOps 디렉터리와 Application 이 남을 수 있기 때문이다. 디렉터리가 이미 없으면 Worker 가 커밋 없이 끝낸다.
- **진행 중인 배포가 있는 서비스가 하나라도 있으면 아무것도 지우지 않고 `409 DEPLOYMENT_IN_PROGRESS` 다.** 한 서비스에 진행 중인 요청은 하나뿐이라 `REMOVE` 를 만들 수 없고, 앱이 떠 있는 채로 서비스만 사라지는 것을 막기 위해서다. 배포는 재시도 한도나 기한 안에 끝나므로 잠시 뒤 다시 지우면 된다.
- Worker 의 `REMOVE` job 은 서비스가 이미 삭제 표시여도 동작한다(job 선점과 처리가 `is_deleted` 를 보지 않는다). 통합 테스트로 확인했다.
- 프로젝트 삭제는 소속 서비스 전체를 검사한 뒤에 만든다. 하나라도 진행 중이면 전체가 `409` 다.
- 삭제된 서비스는 API 에서 조회되지 않으므로 `REMOVE` 의 진행·실패를 사용자가 볼 수 없다. 실패하면(`MANUAL_INTERVENTION`) Worker 로그에 `remove blocked` 가 `ERROR` 로 남는다. 운영자가 로그로 본다.

## 운영 검증 (2026-10-03)
임시 서비스(`delete-e2e`)로 확인했다. `api` 만 배포했다(`main` `1d7cd8d`, 마이그레이션·Worker 변경 없음).

- 배포 중에 `DELETE /services/{id}` 를 보내면 `409 DEPLOYMENT_IN_PROGRESS` 이고 서비스는 그대로 조회된다.
- 배포가 `SUCCEEDED` 가 된 뒤 `DELETE` 는 `204` 이고, 서비스는 곧바로 조회·목록에서 사라진다(`404 SERVICE_NOT_FOUND`).
- GitOps 디렉터리는 삭제 후 약 12초 안에 지워졌고(`remove service 9`), Argo CD Application 은 약 2분 50초 뒤에 사라졌다(삭제 전에는 `Synced/Healthy`, finalizer 있음). 공개 주소는 200 에서 503 으로 바뀌었다. 다른 서비스 Application 은 그대로였다.
- 시험하지 못한 것: 운영에서 프로젝트 삭제(단위·통합 테스트만), 삭제 뒤 `REMOVE` 가 실패하는 경로(`MANUAL_INTERVENTION`), workload 클러스터의 `svc-{id}` namespace 정리 여부.

## 결과
- 서비스·프로젝트를 지우면 앱도 내려가고, 삭제 후 이름을 다시 쓸 수 있다(서비스 id 가 달라 GitOps 경로·도메인이 겹치지 않는다).
- `DELETE` 가 `409 DEPLOYMENT_IN_PROGRESS` 를 줄 수 있다. 클라이언트는 이 오류를 보여 줘야 한다.
- 지운 뒤 `REMOVE` 가 실패하면 앱이 떠 있는 채로 서비스가 보이지 않는다. 이를 줄이려면 `MANUAL_INTERVENTION` 을 알리는 경로(알림)가 필요하다. 후속 과제다.
- ECR 저장소와 `svc-{id}` namespace 는 정리되지 않는다([ADR 0016](0016-remove-service-deployment.md) 의 운영 검증 참고).
- 이 변경 전에 지운 서비스(소프트 삭제만 된 것)는 `REMOVE` 요청이 없다. 필요하면 운영자가 정리한다.
