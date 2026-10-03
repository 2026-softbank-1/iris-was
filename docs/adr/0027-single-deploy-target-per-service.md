# 0027. 서비스는 타깃 하나에만 배포하고 기본은 `aws` 다

- 상태: 수락됨
- 날짜: 2026-10-03
- 결정자: 김현겸

## 배경
[ADR 0006](0006-service-creation-defaults.md) 은 `targetIds` 를 생략하면 모든 타깃에 배포한다고 했지만, Deploy Worker 는 `aws` 타깃 하나에만 배포했다(GitOps 경로·Argo CD Application `svc-{id}` 가 서비스마다 하나다). on-prem 클러스터를 배포 대상으로 쓰게 되면서 타깃 선택을 실제 배포와 맞춰야 했다. 로컬 머신 배포는 하지 않기로 해 `local` 타깃은 on-prem 클러스터 타깃이 대신한다.

[ADR 0014](0014-service-domain-lookup.md) 는 Deploy Worker 가 `BASE_DOMAIN` 으로 주소를 만든다고 했다. 타깃마다 접미사가 다르면 한 설정값으로는 맞출 수 없다.

## 검토한 선택지
1. 여러 타깃에 동시에 배포한다 — release 를 타깃마다 만들고 Application 이름·rollback·REMOVE 를 타깃별로 나눠야 한다. MVP 에는 크다.
2. 서비스당 타깃 하나 — 지금 구조(서비스당 디렉터리·Application 하나)를 유지한다.

## 결정
2 를 택한다.

- 서비스는 타깃을 정확히 하나 갖는다. `targetIds` 를 생략하면 `aws` 다. 2개 이상·빈 목록·없는 타깃은 `422`.
- 배포 요청이 한 번이라도 있으면 타깃을 바꿀 수 없다(`409`). 바꾸면 두 ApplicationSet 이 같은 `svc-{id}` 를 만든다. 바꾸려면 서비스를 지우고 다시 만든다.
- GitOps 경로는 `aws` 가 `services/{id}/prod`(타깃 도입 전 경로를 유지한다. 옮기면 Argo CD 가 Application 을 지웠다 다시 만든다), 그 외는 `services/{id}/{타깃 이름}` 이다.
- 서비스 주소는 Deploy Worker 도 `targets.domain_suffix` 로 계산한다. `BASE_DOMAIN` 설정은 없앤다. 접미사가 없는 타깃엔 배포하지 않는다(`NOT_CONFIGURED`).
- on-prem 타깃은 이름 `onprem`, kind `ONPREM`(온프레미스 클러스터, Tailscale 경유)이다. `local`·`LOCAL` 은 없앤다. 접미사는 `internal.likelion.uk` 다(`*.likelion.uk` 는 AWS ALB 로 간다).
- 마이그레이션이 기존 데이터를 맞춘다. `aws` 와 `onprem` 이 모두 붙은 서비스는 `onprem` 을 떼고, `onprem` 에만 붙었는데 배포 요청이 있는 서비스는 실제로 떠 있는 `aws` 로 옮긴다.

ADR 0006 에서는 "`targetIds` 를 생략하면 모든 타깃" 부분만, ADR 0014 에서는 "Deploy Worker 는 `BASE_DOMAIN`" 부분과 "남은 일 — local 타깃의 도메인" 만 바뀐다. 나머지 결정은 그대로다.

## 결과
- 타깃 선택이 실제 배포 위치와 같다. 도메인 규칙이 `targets.domain_suffix` 한 곳에 모인다.
- 배포한 서비스를 다른 타깃으로 옮기려면 지우고 다시 만들어야 한다(새 id·새 주소).
- on-prem ApplicationSet(`services/*/onprem`)은 iris-infra 가 켠다.
