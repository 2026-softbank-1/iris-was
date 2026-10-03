# 배포 방식 선택 (롤링·카나리·블루그린) SPEC

- 상태: Confirmed (팀 리뷰 동의 2026-10-03)
- 확인일: 2026-10-03
- Ambiguity: Low
- High-impact unresolved decisions: None
- 리뷰 문서: https://app.notion.com/p/3ee8bee9ada481718954cc9a93470a12

## 1. 목표

사용자가 서비스마다 배포 방식을 롤링(기본)·카나리·블루그린 중에서 고른다. 카나리·블루그린은 Pod 가 2개 이상일 때만 쓸 수 있다.

## 2. 현재 맥락과 확인된 사실

- `iris-service` chart 0.6.0 은 일반 `Deployment` 를 쓰고 strategy 가 `RollingUpdate(maxSurge 1, maxUnavailable 0)` 로 고정돼 있다. `values.schema.json` 이 `additionalProperties: false` 라 모르는 키를 넣으면 렌더링이 실패한다.
- Argo Rollouts·Istio·ALB 가중치 라우팅은 없다. AWS 는 공유 ALB Ingress(`target-type: ip`, pod readiness gate)를 쓴다.
- chart 버전은 `helm/gitops/values.yaml` 의 `services.chartRevision` 하나로 전체 서비스에 고정된다. AppProject 화이트리스트에 Rollout 이 없다.
- `PUT /services/{id}/scaling` 은 `replicas` 0~10 을 `services.scaling_config` 에 저장하고 RESTART 배포를 만든다. 값이 없으면 replicas=1 이다. 요청 시점 값은 `deployment_requests.scaling_snapshot` 에 남는다.
- `PATCH /services/{id}` 가 서비스 설정 저장 API 다.
- Release 판정은 Argo health `Healthy`/`Degraded` 와 `deadline_at` 을 따른다. 실패하면 기존 자동 rollback(revert commit)으로 넘어간다.

## 3. 범위

### 포함

- WAS: 방식 저장·검증, 요청 스냅샷(요청 방식·적용 방식), values 생성, deadline 보정, 응답 필드, 용어 사전·ADR·db-schema.sql·openapi 갱신
- infra: Argo Rollouts controller 설치, AppProject 허용, chart 0.7.0(Rollout 리소스·strategy values), 무중단 전환 설정
- web: 방식 선택 UI, Pod 축소 경고 팝업, 배포 상세의 적용 방식 표시, i18n(ko·ja·en)

### 제외

- 트래픽 가중치 라우팅(ALB·Traefik·Gateway), 수동 승격·중단 API, 사용자 정의 단계·대기 시간
- 카나리 단계 실시간 표시, AnalysisTemplate, HPA, iris-cli 지원

## 4. 사용자 흐름

1. 설정 → Deploy 섹션에서 방식을 고른다. 저장된 Pod 수가 2 미만이면 카나리·블루그린은 비활성화되고 안내 문구가 나온다.
2. 저장하면 배포는 만들어지지 않는다. 다음 배포부터 적용된다.
3. 카나리·블루그린 상태에서 Scale 의 Pod 를 2 미만으로 적용하려 하면 경고 팝업(다음 배포부터 롤링으로 대체됨, 취소/계속)이 뜬다.
4. 배포 상세에서 실제 적용된 방식을 보고, 롤링으로 대체됐다면 그 안내를 본다.

## 5. 기능 요구사항

- `FR-1` `services.deployment_strategy`: `ROLLING` · `CANARY` · `BLUE_GREEN`. NOT NULL, 기본값 `ROLLING`.
- `FR-2` `PATCH /services/{id}` 가 `deploymentStrategy` 를 받는다. `ServiceResponse` 에도 포함한다. 배포를 만들지 않고, 배포가 없거나 진행 중이어도 저장한다.
- `FR-3` `CANARY`·`BLUE_GREEN` 저장은 저장된 replicas(없으면 1)가 2 미만이면 `422 INVALID_INPUT`(field `deploymentStrategy`)로 거절한다. Worker 기능 플래그(§7.1)가 꺼져 있어도 같은 422 로 거절한다.
- `FR-4` scaling 변경은 방식과 상관없이 막지 않는다.
- `FR-5` 배포 요청을 만들 때 요청 방식(`requested_deployment_strategy`)과 실제 적용 방식(`deployment_strategy`)을 남긴다. 적용 replicas(scaling 스냅샷, 없으면 1)가 2 미만이면 실제 방식은 `ROLLING` 이다. 새 Pod 를 띄우는 모든 트리거(MANUAL·PUSH·CLI·REDEPLOY·ROLLBACK·RESTART, scaling RESTART 포함)가 같은 규칙을 따른다. REMOVE 는 둘 다 비운다.
- `FR-6` 배포 목록·상세 응답에 `requestedDeploymentStrategy`·`deploymentStrategy` 를 넣는다. 기능 도입 전 요청은 둘 다 null 이다.
- `FR-7` 고정 단계(chart 가 정한다):
  - 롤링: maxSurge 1, maxUnavailable 0
  - 카나리: 새 Pod 1개 → 60초 관찰 → 나머지를 롤링으로 교체
  - 블루그린: 새 묶음 전체 Ready → 30초 뒤 전환 → 이전 묶음 30초 뒤 내림
  - 공통: progressDeadline(`health.timeoutSeconds`)을 넘기면 abort
- `FR-8` Release `deadline_at` 에 방식별 고정 대기 시간(CANARY 60초, BLUE_GREEN 60초)을 더한다.

## 6. 상태와 예외

- 배포 상태 전이는 바꾸지 않는다. 카나리·블루그린 진행 중에도 `DEPLOYING` 이다. Rollout pause 중 Argo health(`Suspended`/`Progressing`)는 WAIT 로 처리한다.
- 실패: 새 Pod 가 Ready 가 되지 않으면 abort → `Degraded` 또는 deadline 초과 → 기존 `FAILED` + 자동 rollback 경로를 탄다.
- replicas 0: 저장된 방식과 상관없이 실제 방식은 ROLLING 이다.

## 7. 레포 간 계약

### 7.1 WAS → chart values

- 키: `deploymentStrategy`, 값 `"ROLLING" | "CANARY" | "BLUE_GREEN"`. chart 는 키가 없으면 `ROLLING` 으로 렌더링한다.
- WAS 설정 `DEPLOYMENT_STRATEGY_ENABLED`(bool, 기본 `false`)가 켜진 Worker 만 이 키를 쓴다. 꺼져 있으면 키를 쓰지 않고, 새 요청의 적용 방식은 `ROLLING` 이다. chart 0.7.0 이 배포된 뒤에 켠다(이전 chart schema 가 모르는 키를 거절한다).

### 7.2 Control API

- `PATCH /api/v1/services/{service_id}` 본문 `deploymentStrategy?: "ROLLING" | "CANARY" | "BLUE_GREEN"`
- `ServiceResponse.deploymentStrategy: "ROLLING" | "CANARY" | "BLUE_GREEN"`
- 배포 목록 항목·배포 상세: `requestedDeploymentStrategy?: ...`, `deploymentStrategy?: ...` (null 이면 생략)
- 거절: `422`, `code: "INVALID_INPUT"`, `details[].field = "deploymentStrategy"`

### 7.3 DB

- `services.deployment_strategy varchar NOT NULL DEFAULT 'ROLLING'` + CHECK
- `deployment_requests.requested_deployment_strategy`, `deployment_requests.deployment_strategy` (nullable) + CHECK
- 모델 → Alembic revision → db-schema.sql 한 세트

### 7.4 infra

- Argo Rollouts controller 를 서비스가 도는 클러스터(AWS·on-prem)에 설치한다(버전 고정).
- AppProject 에 `argoproj.io/Rollout` 을 허용한다.
- chart 0.7.0: `Deployment` 대신 `Rollout`. Service·Ingress·readiness probe 는 그대로 둔다.
- 기존 `Deployment` 는 새 `Rollout` 이 Healthy 가 된 뒤 지운다(무중단).

## 8. 호환성·배포·복구

- 순서: infra(controller → chart 0.7.0 반영) → WAS 배포 → `DEPLOYMENT_STRATEGY_ENABLED=true` → web.
- chartRevision 이 전역이라 chart 0.7.0 반영 시 모든 서비스가 동시에 Deployment → Rollout 으로 바뀐다. 무중단을 지키되 잠시 Pod 수가 늘어난다.
- 블루그린은 배포 중 Pod 가 최대 2배다.
- 블루그린(AWS ALB): 새 묶음은 전환 전 active Service 에 속하지 않아 target 으로 등록되지 않는다. 전환 순간 새 target 이 health check 를 통과할 때까지 몇 초 동안 503 이 날 수 있다. 블루그린 서비스의 Ingress 에만 짧은 health check(5초 간격, healthy threshold 2)를 걸어 이 시간을 줄인다. 근본 해결(ALB traffic routing)은 범위 밖이다. on-prem 은 영향이 없다.
- 되돌리기: 플래그를 끄면 새 배포는 롤링 values 로 돌아간다. chartRevision 을 0.6.0 으로 되돌리려면 먼저 플래그를 끄고, values 에 `deploymentStrategy` 가 남은 서비스를 다시 배포해야 한다. 0.6.0 schema 가 그 키를 거절해 sync 가 실패하기 때문이다(떠 있는 Pod 는 계속 응답한다).

## 9. Acceptance criteria

- `AC-1` 새 서비스의 `deploymentStrategy` 는 `ROLLING` 이다.
- `AC-2` replicas 1 인 서비스에 `CANARY` 를 PATCH 하면 422 이고 값은 바뀌지 않는다.
- `AC-3` replicas 2 에서 `CANARY` 를 저장해도 배포 요청이 생기지 않는다.
- `AC-4` 다음 배포에서 새 Pod 1개만 먼저 Ready 가 되고 60초 뒤 전체가 교체되며 `SUCCEEDED` 다.
- `AC-5` `BLUE_GREEN` 배포는 새 묶음이 전부 Ready 가 되기 전까지 트래픽이 이전 버전으로 간다.
- `AC-6` `CANARY` 상태에서 replicas 를 1 로 줄이면 웹 경고 팝업이 뜨고, 계속하면 그 배포는 적용 `ROLLING`·요청 `CANARY` 로 표시된다.
- `AC-7` 카나리 새 Pod 가 Ready 가 되지 않으면 `FAILED` + 자동 rollback 으로 끝나고 이전 버전 Pod 수가 유지된다.
- `AC-8` chart 0.7.0 반영 중에도 기존 서비스가 계속 200 을 응답한다.

## 10. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D-1 | Argo Rollouts, Pod 비율 방식 | AWS·on-prem 공통 | 사용자 |
| D-2 | 자동 승격 | 기존 상태 흐름 재사용 | 사용자 |
| D-3 | 단계·대기 시간 고정값 | 입력·검증 최소화 | 사용자 |
| D-4 | 다음 배포부터 적용 | 방식은 새 버전을 올릴 때만 의미 | 사용자 |
| D-5 | 2 미만 저장 거절, 축소 시 롤링 대체 + 웹 경고 팝업 | 서비스 중지를 막지 않음 | 사용자 |
| D-6 | 롤링 포함 전부 Rollout | 방식 전환이 값 변경뿐 | 사용자 |
| D-7 | 화면은 적용 방식만 표시 | 범위 축소 | 사용자 |
| D-8 | 저장 API 는 `PATCH /services/{id}` | 서비스 설정 저장 관례 | AI default |
| D-9 | 요청·적용 방식 둘 다 스냅샷 | 대체 안내·이력 | AI default |
| D-10 | 모든 Pod 교체 트리거에 같은 규칙 | 예외 경로 축소 | AI default |
| D-11 | chart 전역 동시 전환 수용 | 서비스별 chart 버전은 범위가 큼 | AI default, 팀 동의 |
| D-12 | WAS 기능 플래그 `DEPLOYMENT_STRATEGY_ENABLED` | 레포별 병합·배포 순서를 독립시킴 | AI default |

## 11. 구현 계획으로 위임한 세부사항

- chart 의 카나리 weight 계산식(새 Pod 정확히 1개), 블루그린 preview Service 여부
- 이전 Deployment 무중단 정리 방법(PruneLast 등)
- 경고 팝업 문구, UI 배치
