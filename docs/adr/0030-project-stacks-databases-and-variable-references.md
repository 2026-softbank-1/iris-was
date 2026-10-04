# 0030. 한 레포의 여러 이미지를 스택으로 묶어 의존 순서로 반복 배포하고, 개발용 DB·참조 변수·호스트 별칭을 붙인다

- 상태: 제안됨 (iris-infra chart 0.8.0 AWS pin 반영 후 `PROJECT_NETWORKING_ENABLED` 켬 대기)
- 날짜: 2026-10-04
- 결정자: 김우현
- 근거: 통합 설계 `DESIGN-phase2-databases-env.md`(계약 A chart 0.8.0 · 계약 B 분석기 · 계약 C·E WAS), ADR 0029

## 배경
ADR 0029 로 compose 같은 멀티 이미지 레포를 unit 마다 서비스로 만들 수 있게 됐다. 하지만 한 번 만들고 끝나면 운영할 수 없다.

- 서비스마다 namespace `svc-{id}` 이고 egress 정책이 VPC 안(다른 Pod)을 막아 web→api, api→DB 가 안 된다. 코드는 compose 호스트명(`api:3000`, `postgres:5432`)을 그대로 쓴다.
- DB·Redis 는 분석기가 찾지만 플랫폼이 만들지 않았다. 사용자가 연결 정보를 손으로 넣다 `localhost` 를 넣는 일이 많다(배포 뒤에야 실패).
- 서비스마다 따로 배포하면 앱이 DB 보다 먼저 떠 CrashLoop 에 빠진다. push 는 경로가 바뀐 앱을 아무 순서로 다시 빌드한다.
- 레포 구성이 바뀌어도(새 unit·의존성·포트) 알 길이 없다.

## 검토한 선택지
1. 스택 = 최초 분석 id 를 서비스에 기록(테이블 없음) — 재분석 기준·변경 감지·스택 배포 기록을 둘 곳이 없다.
2. `service_stacks` 테이블 + 서비스 `stack_id`·`stack_unit_id` — 증분 apply 의 매칭 기준(unit id)과 재분석 기준(analysis_id)·pendingChanges 를 한 행에 둔다.
3. 순서 배포: 모든 요청을 동시에 시작하고 앱이 DB 를 기다리게 한다 — 앱 코드에 재시도를 강요하고 실패가 진단 노이즈가 된다.
4. 순서 배포: 요청을 미리 만들어 QUEUED 로 두고(job 없음), 앞 단계가 SUCCEEDED 가 되는 트랜잭션에서 다음 단계의 첫 job 을 만든다.

## 결정
2 와 4 를 택한다.

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | `service_stacks`(project·저장소·브랜치·위치, 기준 `analysis_id`, `pending_changes`)와 `services.stack_id`·`stack_unit_id`(스택 안 unit·dependency id 유일)를 둔다 | 증분 apply 는 unit id 로 기존 서비스를 찾아 고치고 새 것만 만든다(중복 없음). 사라진 unit 은 지우지 않고 `UNIT_REMOVED` 로만 보인다 |
| D-2 | 의존 그래프 = 호스트 별칭 대상 ∪ 참조 변수 대상 ∪ 분석기 `dependsOn`. 순서는 깊이 + 1 (DB 1 → DB 를 쓰는 앱 2 → 그 앱을 쓰는 앱 3) | 사용자가 별칭·참조를 고치면 순서도 따라간다 |
| D-3 | 스택 배포(`stack_deployments`·`stack_deployment_steps`)는 서비스마다 기존 경로로 배포 요청을 만든다. 앞 단계가 없는 요청만 job 을 만들고 나머지는 QUEUED(WAITING). `DeploymentStatusService.transition_status` 가 끝난 상태로 옮길 때 같은 트랜잭션에서 기다리던 단계를 잠그고 앞 단계가 모두 SUCCEEDED 면 첫 job 을 만든다. 실패·되돌림·운영자 개입·대체면 뒤 단계를 HELD 로 두고 요청을 `FAILED`·`DEPENDENCY_FAILED` 로 끝낸다(재귀적으로 전파) | 상태 전이의 유일한 경로라 Build·Deploy Worker 어느 쪽에서 끝나도 빠짐없이 진행된다. 원자적이고 at-least-once 재처리에도 한 번만 시작된다(단계 행 잠금 후 새 문장으로 앞 단계 상태를 읽는다). 대기 중 요청이 진행 중 index 를 잡아 다른 배포가 끼어들지 못한다 |
| D-4 | 관리형 DB 는 `services.kind=DATABASE`. 빌드 없이 고정 공식 이미지(digest 고정, `DATABASE_IMAGES` 로 바꿈)를 가리키는 성공한 `builds` 행을 붙여 바로 DEPLOY 한다. values 는 chart 0.8.0 `workload.kind: database` | release·reconcile·rollback·remove 를 그대로 쓴다. 삭제는 기존 REMOVE 가 GitOps 디렉터리를 지우고 chart 가 PVC 까지 지운다(데이터 소실, 백업 없음) |
| D-5 | 자격 증명은 만들 때 한 번 생성해 DB 서비스의 암호화 변수(엔진별 이름)로 둔다. 변수 API 에서 고칠 수 없고 목록 대신 `systemVariables` 에 이름만(비밀번호는 값 없이) 보인다 | 기존 봉인 경로로 전달된다. 비밀번호는 어떤 응답·로그에도 나가지 않는다 |
| D-6 | 참조 변수 `service_variables.reference = {serviceId, property}`(값과 참조 중 하나, CHECK). 스냅샷에는 `{key: {"reference": …}}` 로 남고 Deploy Worker 가 봉인 직전에 대상의 지금 값으로 푼다. 응답에는 비밀을 가린 `resolved` 미리보기 | 교차 namespace Secret 참조가 불가하므로 앱 namespace 의 SealedSecret 에 복사한다. 평문은 Worker 메모리에만 있다 |
| D-7 | 배포 요청(MANUAL·CLI·REDEPLOY·RESTART·스택 재배포·apply)을 만들기 전에 환경변수를 검증해 error 면 `422 VARIABLES_INVALID`(`details`=키·코드, `data`={ok, issues}). 오탐 우회 `skipVariableValidation`. push 자동 배포는 요청을 `FAILED`·`VARIABLES_INVALID` 로 남긴다(빌드 안 함). 분석 정보가 없는 기존 서비스는 필수 키 검사를 하지 않는다 | 확실히 실패할 배포를 미리 막는다. 시작 전 실패 두 코드는 로그가 없어 자동 진단하지 않는다 |
| D-8 | push 가 스택 레포에 오면 경로가 바뀐 앱만 스택 배포(PUSH)로 순서대로 다시 빌드하고, 같은 커밋으로 force 모드 재분석을 접수한다(스택·커밋 유일). Build Worker 가 끝낸 재분석은 기준 분석과 비교해 `pending_changes` 를 남기고, 그 분석을 apply 하면 기준이 되어 지워진다 | DB 는 push 로 다시 배포하지 않는다. 사용자가 고르지 않은 unit 이 매번 "추가"로 보이지 않게 서비스가 아니라 기준 분석과 비교한다 |
| D-9 | chart 0.8.0 키(`projectId`·`service.exposeContainerPort`·`hostAliases`·`workload`·`database`)는 `PROJECT_NETWORKING_ENABLED` 를 켠 Worker 가 AWS 타깃 release 에만 쓴다. 꺼져 있거나 on-prem 이면 values 가 이전과 바이트까지 같고, DB 생성·별칭·참조 변수는 422 다. 스택 앱의 `containerPort` 는 분석한 포트(없으면 8080)라 `api:3000` 이 그대로 풀린다 | 0.7.1 schema 는 모르는 키를 거절한다. pin 과 WAS 배포 순서를 설정으로 끊는다 |

## 결과
- 롤아웃 순서: ① iris-infra chart 0.8.0 + AWS ApplicationSet pin(0.8.0) 적용 ② WAS 이미지 배포(`alembic upgrade head`: `6b1f0c2d9a41`, `c4e2a7b81f30`, 초기화 스크립트 `663b24ad4296`) ③ Control API·Deploy Worker 에 `PROJECT_NETWORKING_ENABLED=true`. ③ 뒤 첫 배포에서 기존 AWS 앱에도 `projectId` 라벨이 붙어 Pod 가 한 번 다시 뜬다(롤링). 다른 서비스 values 는 그대로다.
- on-prem 은 chart 0.6.0 에 남는다. 스택 순서 배포는 on-prem 에서도 동작하지만 DB·별칭·참조 변수는 쓰지 않는다.
- ponytail: 스택 배포 단계는 순서만 보장하고 앱의 런타임 readiness(예: DB 마이그레이션 완료)는 보지 않는다. DB Ready 는 release SUCCEEDED(Argo Healthy = TCP readiness)다.
- ponytail: DB 비밀번호 교체·용량 변경은 지원하지 않는다(StatefulSet volumeClaimTemplates 고정).
- 추가(2026-10-04): apply 로 만든 DB 는 레포의 `/docker-entrypoint-initdb.d` 스크립트를 첫 기동에 한 번 실행한다(chart 0.8.0 `database.initScripts`). 이미 있는 DB 에는 다시 실행하지 않고 `DEPENDENCY_CHANGED` 로만 알린다. [ADR 0031](0031-database-init-scripts.md)
