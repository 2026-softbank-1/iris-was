# 0028. 서비스마다 배포 방식(롤링·카나리·블루그린)을 고르고, Pod 가 2개 미만이면 롤링으로 대체한다

- 상태: 제안됨 (iris-infra chart 0.7.0·Argo Rollouts 반영 대기)
- 날짜: 2026-10-03
- 결정자: 김지민 (팀 리뷰 동의 2026-10-03)
- 근거: [specs/deployment-strategy-selection.md](../../specs/deployment-strategy-selection.md)

## 배경
`iris-service` chart 0.6.0 은 일반 `Deployment` 를 쓰고 strategy 가 `RollingUpdate(maxSurge 1, maxUnavailable 0)` 로 고정돼 있다. 사용자가 서비스마다 새 버전을 올리는 방식을 고르게 하고 싶다. Argo Rollouts·Istio·ALB 가중치 라우팅은 없고, AWS 는 공유 ALB Ingress(`target-type: ip`)를 쓴다. chart 버전은 `services.chartRevision` 하나로 전체 서비스에 고정되고, chart 의 values schema 는 모르는 키를 거절한다.

Pod 수는 `PUT /services/{id}/scaling` 이 `services.scaling_config` 에 저장하고 배포 요청마다 `scaling_snapshot` 으로 고정한다. 값이 없으면 replicas 1 이다.

## 검토한 선택지
1. 트래픽 가중치 라우팅(ALB·Gateway)으로 카나리 비율을 정한다 — AWS·on-prem 이 라우팅 수단이 달라 두 벌을 만들어야 한다.
2. Argo Rollouts 의 Pod 비율 방식 — 같은 chart 가 두 클러스터에서 그대로 동작한다. 정확한 트래픽 비율은 Pod 수에 따른다.
3. 수동 승격·사용자 정의 단계 — 입력·검증·API 가 늘고 기존 상태 흐름(`DEPLOYING` → 판정)을 바꿔야 한다.

## 결정
2 를 택하고 단계는 고정한다.

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | Argo Rollouts, Pod 비율 방식 | AWS·on-prem 공통 |
| D-2 | 자동 승격 | 기존 상태 흐름(`DEPLOYING` → Argo health 판정)을 그대로 쓴다 |
| D-3 | 단계·대기 시간은 chart 가 정한 고정값 | 입력·검증을 최소로 한다 |
| D-4 | 방식은 다음 배포부터 적용한다. 저장만 하고 배포를 만들지 않는다 | 방식은 새 버전을 올릴 때만 의미가 있다 |
| D-5 | replicas 2 미만이면 `CANARY`·`BLUE_GREEN` 저장을 거절(`422 INVALID_INPUT`, field `deploymentStrategy`)한다. 저장 뒤 Pod 를 줄이는 것은 막지 않고, 그 배포는 롤링으로 대체한다(웹이 경고 팝업) | 서비스 중지(replicas 0)를 막지 않는다 |
| D-6 | 롤링을 포함해 모든 서비스를 `Rollout` 으로 배포한다 | 방식 전환이 values 변경뿐이다 |
| D-7 | 화면은 적용 방식만 보인다 | 카나리 단계 실시간 표시는 범위 밖이다 |
| D-8 | 저장 API 는 `PATCH /services/{id}` 의 `deploymentStrategy` | 서비스 설정 저장 관례 |
| D-9 | 배포 요청에 요청 방식(`requested_deployment_strategy`)과 적용 방식(`deployment_strategy`)을 둘 다 남긴다 | 롤링 대체 안내와 이력 |
| D-10 | 새 Pod 를 띄우는 모든 트리거(MANUAL·PUSH·CLI·REDEPLOY·ROLLBACK·RESTART, scaling RESTART 포함)가 같은 규칙을 따른다. REMOVE 는 둘 다 비운다 | 예외 경로를 줄인다 |
| D-11 | chart 0.7.0 반영 때 모든 서비스가 동시에 `Deployment` → `Rollout` 으로 바뀌는 것을 받아들인다 | 서비스별 chart 버전은 범위가 크다 |
| D-12 | WAS 기능 플래그 `DEPLOYMENT_STRATEGY_ENABLED`(기본 `false`) | 레포별 병합·배포 순서를 독립시킨다 |

WAS 동작:

- `services.deployment_strategy`(NOT NULL, 기본 `ROLLING`). 응답 `ServiceResponse.deploymentStrategy`.
- 배포 요청을 만들 때 서비스 행을 잠근 채 Pod 설정과 방식을 함께 읽는다. 적용 방식은 `resolve_deployment_strategy`(`app/services/deployment_strategy.py`)가 정한다. 플래그가 꺼져 있거나 적용 replicas 가 2 미만이면 `ROLLING` 이다.
- 플래그가 꺼져 있으면 `CANARY`·`BLUE_GREEN` 저장도 같은 `422` 로 거절한다(reason `deployment_strategy_disabled`, replicas 부족은 `at_least_two_replicas_required`). 이미 저장된 값과 같은 값을 다시 보내면 검사하지 않는다.
- 플래그를 켠 Deploy Worker 만 values 에 `deploymentStrategy` 를 쓴다(값이 없는 기능 도입 전 요청은 `ROLLING`). 키가 없으면 chart 가 `ROLLING` 으로 렌더링한다.
- release `deadline_at` 에 방식별 고정 대기(`CANARY`·`BLUE_GREEN` 60초)를 더한다. 자동 revert 의 기한은 되돌아가는 이전 정상 release 의 방식을 따른다.
- 배포 상태 전이는 바꾸지 않는다. Rollout 이 멈춘 동안의 Argo health `Suspended`·`Progressing` 은 대기이고, `Degraded`·기한 초과는 기존 `FAILED` + 자동 rollback 경로를 탄다.

## 결과
- 배포 순서: iris-infra(Argo Rollouts controller → chart 0.7.0) → WAS 배포 → `DEPLOYMENT_STRATEGY_ENABLED=true`(Control API·Deploy Worker 모두) → web. 플래그를 chart 0.7.0 전에 켜면 이전 chart schema 가 모르는 키를 거절해 모든 배포가 실패한다.
- 되돌리기: 플래그를 끄면 새 배포는 롤링 values 로 돌아간다. chart 는 `chartRevision` 을 0.6.0 으로 되돌린다.
- 블루그린은 배포 중 Pod 가 최대 2배다. 카나리 트래픽 비율은 Pod 수에 따라 정확하지 않다.
- 수동 승격·중단, 사용자 정의 단계, 카나리 단계 실시간 표시, AnalysisTemplate 은 범위 밖이다.
