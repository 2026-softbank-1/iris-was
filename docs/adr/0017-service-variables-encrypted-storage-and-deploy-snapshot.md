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
- API: 목록(`GET`, 자동 주입 변수 포함)·추가(`POST`)·값 수정(`PUT /{key}`)·삭제(`DELETE /{key}`)·Raw 일괄 저장(`PUT`, 서비스의 변수 전체를 텍스트로 교체). Raw 는 큰따옴표 값을 JSON 이스케이프로 읽고 따옴표 값은 닫는 따옴표까지 여러 줄에 걸칠 수 있다. 받을 수 없는 줄은 422 `INVALID_INPUT` 의 `details`(`field=raw`, `reason="line 7: reserved key PORT"`)로 줄 번호와 사유를 알리며 거부한다(전부 반영하거나 하나도 반영하지 않는다). 키 규칙 위반은 틀린 줄을 모두 싣고(최대 20개), 따옴표가 닫히지 않으면 어디까지가 그 값인지 알 수 없어 첫 오류에서 멈춘다.
- 자동 주입 변수 `PORT`·`IRIS_SERVICE_NAME`·`IRIS_TARGET_NAME`·`IRIS_DEPLOYMENT_ID`·`IRIS_PUBLIC_DOMAIN`·`IRIS_GIT_COMMIT_SHA` 는 저장하지 않고 이름·설명(과 서비스만으로 정해지는 값)을 계산해 돌려준다.
- 배포 요청을 만들 때(`DeploymentRequestService`) `variables_snapshot` 에 암호문을 복사한다. 웹훅·수동 경로가 같은 로직을 쓴다. `ROLLBACK` 은 원본 요청의 스냅샷을 쓰고(없던 옛 요청이면 지금 변수), 나머지(`MANUAL`·`PUSH`·`REDEPLOY`·`RESTART`)는 그 시점의 변수를 쓴다. `REMOVE` 는 원본의 스냅샷을 그대로 둔다(내리는 요청이라 쓰이지 않는다).
- 키는 설정 `VARIABLES_ENCRYPTION_KEY`(SecretStr)로 주입한다. 없으면 변수 API 만 `503 NOT_CONFIGURED` 다.

## 결과
- 프런트는 localStorage 대신 이 API 로 변수를 저장·조회할 수 있다.
- 변수를 고치는 것만으로는 앱이 바뀌지 않는다. 새 배포 요청(재배포·재시작 포함)의 스냅샷에 반영된다.
- **앱 전달 (2026-10-03)**: iris-infra 가 `iris-service` 0.6.0(`variables`·`iris` values, SealedSecret, `envFrom`)과 Sealed Secrets controller addon 을 만들었다([iris-infra ADR 0004](https://github.com/2026-softbank-1/iris-infra/blob/main/docs/decisions/0004-user-variables-sealed-secrets.md)). Deploy Worker 는 DEPLOY 때 스냅샷을 `VARIABLES_ENCRYPTION_KEY` 로 풀어 controller 공개 인증서(`SEALED_SECRETS_CERT`)로 다시 봉인한다. strict scope 로 namespace `svc-{service_id}`, Secret 이름 `vars-r{release_id}` 에 묶고, 결과를 `values.yaml` 의 `variables.name`·`variables.encryptedData` 로 커밋한다. 서비스·타깃 이름과 배포 요청 id 는 `iris` 로 넘겨 env `IRIS_SERVICE_NAME`·`IRIS_TARGET_NAME`·`IRIS_DEPLOYMENT_ID` 가 된다.
- **켜는 조건**: `SEALED_SECRETS_CERT` 를 설정해야 Worker 가 `iris`·`variables` 를 values 에 쓴다. 이전 chart(0.5.0)의 schema 는 모르는 키를 거절하므로, 클러스터가 chart 0.6.0 으로 올라간 뒤에 설정한다. 설정 전에는 이전과 같은 values 를 쓰고, 변수가 있는 서비스의 배포만 실패한다(변수 없이 뜨지 않게).
- 봉인은 `kubeseal` 실행 파일 없이 `cryptography` 로 직접 한다(`app/clients/secret_sealer.py`, RSA-OAEP SHA-256 + AES-256-GCM, label `{namespace}/{name}`). 실제 `kubeseal` 0.40.0 의 복호화로 값이 그대로 풀리고 다른 namespace·이름은 거절되는 것을 확인했다(일회성 검증, 테스트에는 같은 형식의 복호화 도우미를 쓴다).
- 변수가 있는데 키나 인증서가 없거나 복호화가 안 되면, 변수 없이 배포하지 않고 그 배포를 실패시킨다(`DEPLOY_INFRA_ERROR`, GitOps 는 바뀌지 않는다).
- 자동 롤백은 이전 commit 으로 되돌리는 방식이라 다시 봉인하지 않는다. 사용자가 시작하는 롤백·재시작·재배포는 새 release 라서 스냅샷에서 새로 봉인한다.
- **Secret 공유**: Deploy Worker 가 복호화 키를 받는다. 지금은 모든 WAS 컴포넌트가 `.env` Secret 하나를 `envFrom` 으로 받아 별도 설정이 필요 없지만, "컴포넌트끼리 Secret 을 공유하지 않는다" 원칙과는 어긋난다. Worker 전용 키·KMS 로 나누는 것은 후속이다.
- **운영 반영 (2026-10-03)**: Argo root 를 iris-infra `bba1453` 으로 옮겨 chart 0.6.0 과 controller 를 반영했고, controller 키를 Secrets Manager `iris/dev/sealed-secrets-key` 에 백업했으며, 공개 인증서를 Secret `iris-platform-was-env` 의 `SEALED_SECRETS_CERT` 로 추가해 Deploy Worker 를 재시작했다. 기능은 켜진 상태다.
- **클러스터에서 확인한 것**: 임시 namespace 에서 `SecretSealer` 가 만든 값을 controller 가 원문 그대로 풀었고(여러 줄·한글·URL), 빈 값(`KEY=`)도 빈 Secret 값으로 풀리며 `envFrom` 으로 읽은 Pod 의 환경변수도 같았다. 다른 namespace 용으로 봉인한 값은 거절됐다. 앞서 걱정한 `kubeseal` JSON 의 빈 값 `null` 은 도구 출력 표현일 뿐이었다.
- **end-to-end 확인 (2026-10-03)**: 시험 서비스(`railway-deploy-demo` 저장소)에서 API 로 변수 4개(여러 줄·한글·빈 값 포함)를 등록하고 배포했다. 실행 중 Pod 가 값을 모두 원문 그대로 받았고(해시 비교), GitOps `values.yaml` 에는 봉인된 값만 있었으며 평문 조각은 없었다. `IRIS_SERVICE_NAME`·`IRIS_TARGET_NAME`·`IRIS_DEPLOYMENT_ID` 도 주입됐다.
  - 변수를 고치고(값 변경·삭제·추가) **RESTART** 하니 새 값이 반영됐고 삭제한 변수는 사라졌으며 이전 Secret 은 정리됐다.
  - 첫 배포로 **ROLLBACK** 하니 그때의 변수가 복원됐다. 롤백은 서비스의 현재 변수가 아니라 배포의 스냅샷을 되돌리므로, 롤백 직후에는 화면의 현재 변수와 실행 중인 값이 다를 수 있다.
  - **REMOVE** 로 클러스터 리소스와 GitOps 디렉터리가 정리됐다.
- **확인하지 못한 것**: 실패한 release 를 되돌리는 자동 rollback(revert commit) 경로에서의 변수 복원, REDEPLOY(스냅샷 로직은 RESTART 와 같고 단위 테스트가 덮는다).
- 암호화 키를 잃으면 저장된 값을 읽을 수 없다(서비스를 다시 입력해야 한다). 키 교체(rotation)는 지원하지 않는다. 필요해지면 `MultiFernet` 으로 확장한다.
