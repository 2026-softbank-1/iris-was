# 0017. 서비스 환경변수는 암호화해 DB 에 저장하고 배포 요청마다 스냅샷을 남긴다

- 상태: 제안됨
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
Notion task "[API] 환경변수 API"는 변수 CRUD, Raw(`.env`) 일괄 저장, 자동 주입 변수 조회, 배포 시 스냅샷 저장을 요구한다. 완료 기준은 "변수를 고치고 재배포하면 앱에 반영된다"이다.

코드·인프라 현황은 이렇다.

| 대상 | 상태 |
|---|---|
| 저장소 | 변수 테이블이 없다. `deployment_requests.variables_snapshot`(jsonb) 컬럼만 있고 쓰는 곳이 없다 |
| 웹 | Variables 탭이 값을 브라우저 localStorage 에만 둔다. Raw 편집기는 `KEY="값"`(JSON 문자열) 줄을 읽고 쓴다. 값 보기·복사 버튼이 있어 API 가 값을 돌려줘야 한다 |
| chart(iris-infra `iris-service` 0.2.0) | `values.schema.json` 이 모르는 키를 거절한다. 사용자 변수를 받는 값이 없고, env 는 `PORT`·`IRIS_PUBLIC_DOMAIN`·`IRIS_GIT_COMMIT_SHA` 만 넣는다 |
| 전달 경로 | Deploy Worker 는 Prod 에 접근하지 않고 Git 에 커밋만 한다. 평문 변수를 Git 에 올리지 않는다(Sealed Secrets 안, `.claude/docs/todo-runtime-variables.md`) |

## 검토한 선택지
저장:
1. 평문으로 저장한다 — 단순하다. DB 유출·백업·운영자 조회에 값이 그대로 드러난다.
2. 애플리케이션에서 Fernet 으로 암호화해 저장한다 — 키(`VARIABLES_ENCRYPTION_KEY`) 하나만 주입하면 되고 KMS·인프라 변경이 없다. 키를 잃으면 값을 못 읽는다.
3. KMS 로 암호화한다(todo 의 원안) — 키 관리가 안전하다. Control API IAM·KMS 키·encryption context 가 필요해 인프라 작업이 선행된다.

스냅샷 내용:
1. 평문 복사 — 스냅샷이 DB 에 평문으로 남는다.
2. 암호문 복사(`{key: encrypted_value}`) — 복호화 키 없이 복사할 수 있고 평문이 늘지 않는다.

재배포·재시작·롤백의 변수:
1. 모두 원본 요청의 스냅샷을 쓴다 — 롤백은 맞지만 "변수를 고친 뒤 재배포·재시작"이 고친 값을 못 쓴다.
2. 롤백만 원본 스냅샷, 재배포·재시작은 지금 변수 — 완료 기준과 롤백 의미를 둘 다 만족한다.

## 결정
저장은 2, 스냅샷은 2, 변수 선택은 2 를 택한다.

- 테이블 `service_variables(service_id, key, encrypted_value)`, `(service_id, key)` 유일. 값은 Fernet 암호문이다. 응답은 소유자에게 복호화한 값을 준다(웹이 값 보기·복사를 한다). 로그에는 키·값을 남기지 않고 변경 건수만 남긴다.
- 키는 영문·숫자·밑줄(숫자로 시작 금지, 128자 이하). 플랫폼이 쓰는 `PORT`·`IRIS_*` 는 만들 수 없다(chart 의 env 가 우선해 덮어쓸 수 없는데 저장만 되면 혼동된다). 서비스당 100개, 값 32KiB 가 상한이다.
- API: 목록(`GET`, 자동 주입 변수 포함)·추가(`POST`)·값 수정(`PUT /{key}`)·삭제(`DELETE /{key}`)·Raw 일괄 저장(`PUT`, 서비스의 변수 전체를 텍스트로 교체). Raw 는 큰따옴표 값을 JSON 이스케이프로 읽고 형식이 틀린 줄은 줄 번호와 함께 거부한다(전부 반영하거나 하나도 반영하지 않는다).
- 자동 주입 변수 `PORT`·`IRIS_SERVICE_NAME`·`IRIS_TARGET_NAME`·`IRIS_DEPLOYMENT_ID`·`IRIS_PUBLIC_DOMAIN`·`IRIS_GIT_COMMIT_SHA` 는 저장하지 않고 이름·설명(과 서비스만으로 정해지는 값)을 계산해 돌려준다.
- 배포 요청을 만들 때(`DeploymentRequestService`) `variables_snapshot` 에 암호문을 복사한다. 웹훅·수동 경로가 같은 로직을 쓴다. `ROLLBACK` 은 원본 요청의 스냅샷을 쓰고(없던 옛 요청이면 지금 변수), 나머지(`MANUAL`·`PUSH`·`REDEPLOY`·`RESTART`)는 그 시점의 변수를 쓴다. `REMOVE` 는 원본의 스냅샷을 그대로 둔다(내리는 요청이라 쓰이지 않는다).
- 키는 설정 `VARIABLES_ENCRYPTION_KEY`(SecretStr)로 주입한다. 없으면 변수 API 만 `503 NOT_CONFIGURED` 다.

## 결과
- 프런트는 localStorage 대신 이 API 로 변수를 저장·조회할 수 있다.
- 변수를 고치는 것만으로는 앱이 바뀌지 않는다. 새 배포 요청(재배포·재시작 포함)의 스냅샷에 반영된다.
- **아직 앱에 전달되지 않는다.** chart 가 사용자 변수를 받지 않고(값 스키마가 거절), Prod 에 Secret 을 만들 경로(Sealed Secrets controller)가 없다. 스냅샷을 읽어 전달하는 일은 후속이다 — Worker 가 암호문을 복호화해 `kubeseal` 로 봉인하고 chart 가 `SealedSecret`·`envFrom` 을 만드는 안이 `.claude/docs/todo-runtime-variables.md` 에 있다. 이 ADR 의 스냅샷(암호문)을 그 입력으로 쓴다. Deploy Worker 가 복호화하려면 같은 키를 받아야 하므로 "컴포넌트끼리 Secret 을 공유하지 않는다" 원칙과 맞추는 방식(예: Worker 전용 키·KMS)을 그때 정한다.
- `IRIS_SERVICE_NAME`·`IRIS_TARGET_NAME`·`IRIS_DEPLOYMENT_ID` 는 chart 가 아직 넣지 않는다. 이름은 이 API 와 chart 의 계약이므로 iris-infra 쪽 반영이 필요하다.
- 암호화 키를 잃으면 저장된 값을 읽을 수 없다(서비스를 다시 입력해야 한다). 키 교체(rotation)는 지원하지 않는다. 필요해지면 `MultiFernet` 으로 확장한다.
