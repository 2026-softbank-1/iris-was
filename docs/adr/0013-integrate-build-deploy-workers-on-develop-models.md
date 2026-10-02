# 0013. main 의 Build·Deploy Worker 를 develop 모델 위에 통합하고 옛 마이그레이션을 걷어낸다

- 상태: 수락됨 (현겸님 사후 확인 대기)
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
같은 기반(users·services·deployment_requests·jobs·builds·releases 테이블)을 두 갈래에서 따로 만들었다.

| | develop (김지민) | main (현겸) |
|---|---|---|
| 내용 | 로그인·프로젝트·서비스·웹훅·배포 요청 API | Build·Deploy Worker (CodeBuild·GitOps·Argo CD) |
| 규모(통합 전) | API 2,681줄 + 테스트 3,854줄, 라우터 8개 | Worker 1,922줄 + 테스트 1,456줄 |
| 스키마 | 12개 테이블 (projects·targets·상태 이력·GitHub 설치 연결 포함) | Worker 가 돌 만큼의 최소 6개 테이블 |
| 기준 문서 | 용어 사전, ADR 0004·0010 | 현겸님 계획 문서(`.claude/docs/deploy-worker-plan.md`) |

시간 순서(UTC)는 이렇다.
- 09-30 16:02 골격(#1)이 `main` 에 머지됐다.
- 10-01 11:20 `develop` 에 스키마·API(#2)가 머지됐다.
- 10-01 13:30 Build Worker PR(#5)이 `main` 으로 올라왔고, Deploy Worker(#8)는 16:25 에 올라왔다.

현겸님은 `main` 을 기준으로 작업했다. 그 시점 `main` 의 `git-conventions.md` 에는 `develop` 에서 시작한다는 내용이 없었다(`develop` 이라는 단어 0회). 브랜치 전략(ADR 0008)이 `develop` 에만 있었기 때문이다. 두 갈래는 서로의 작업이 보이지 않은 채 같은 이름의 테이블을 다른 컬럼으로 만들었고, Alembic 체인도 각자 `down_revision=None` 에서 시작했다. 그대로 합치면 18개 파일이 충돌한다(모델 6개 add/add 포함). 충돌을 풀어도 `develop` API 가 BUILD job payload 에 소스 정보를 넣는 반면 `main` Worker 는 `payload["build_id"]` 를 읽어서 빌드가 시작되지 않는다.

## 검토한 선택지
1. develop 의 API 를 현겸님 테이블 위로 이식 — 가능하지만 API 2,681줄·테스트 3,854줄·라우터 8개를 다시 짜야 하고, `projects`·`targets`·상태 이력·GitHub 설치 연결이 없어서 만들어야 한다.
2. main 의 Worker 를 develop 모델에 이식 — Worker 가 모델에서 읽는 필드는 `service` 8개, `deployment_request` 5개뿐이라 맞출 부분이 작다.
3. Worker 를 빼고 develop 만 main 에 올림 — 현겸님의 Worker 가 `main` 에서 사라진다. 그렇게 해도 `main` 의 Worker 는 BUILD job 을 만드는 코드가 없어 단독으로는 돌 수 없었다.
4. 두 스키마를 함께 둠 — 같은 개념(사용자·서비스·배포 요청)이 두 벌이 된다.

## 결정
2번. 용어 사전이 최우선 규칙이고(`CLAUDE.md`: 용어 사전 > 컨벤션 > 관례), `develop` 의 모델은 ADR 0004(빌드는 요청당 1건·릴리스는 타깃마다 1건)와 ADR 0010(상태는 전이 함수로만)을 따른다. Worker 의 핵심 로직(CodeBuild → ECR digest → GitOps 커밋 → Argo CD 판정 → revert 롤백)은 그대로 두고 모델 접점만 맞춘다.

### Worker 쪽에서 바뀐 것과 근거

| # | 변경 | 근거 |
|---|---|---|
| 1 | 배포 요청을 만들 때 `builds` 행을 같이 만들고 BUILD job payload 를 `{"build_id"}` 로 한다 | Worker 의 재개·재시도(`codebuild_build_id` 를 먼저 기록)가 `builds` 행을 중심으로 설계돼 있다. API 쪽을 Worker 방식에 맞췄다 |
| 2 | 요청 `status` 는 `DeploymentStatusService` 로만 바꾼다. `Build`·`Release` 모델의 상태 메서드는 요청 status 를 건드리지 않는다 | ADR 0010·용어 사전 §4.2. 허용 전이와 이력을 보장한다. 이 변경으로 빌드 시작·성공·실패마다 이력이 한 줄씩 남는다 |
| 3 | `INITIALIZING` 상태를 두지 않는다. 소스 스냅샷 동안 요청은 `QUEUED`, CodeBuild 를 시작할 때 `BUILDING` | 용어 사전: 화면 Initializing = `QUEUED` |
| 4 | `SUPERSEDED` 상태와 `deployment_requests.cancel_requested_at` 을 추가했다 | 현겸님 Worker 의 취소 처리를 보존하기 위해서다. **ADR 0010 의 "상태 값은 늘리지 않는다"와 어긋난다.** 또한 현재 `develop` API 는 진행 중 요청이 있으면 새 요청을 만들지 않아 `cancel_requested_at` 을 쓰는 곳이 없다(휴면 경로) |
| 5 | 롤백 대기 중인 요청은 `FAILED`(+failure_code) 로 두고 되돌림이 끝나면 `ROLLED_BACK` 으로 옮긴다 | 허용 표에 이미 `FAILED → ROLLED_BACK` 이 있고 요청에 failure_code 가 남는다. 되돌리는 동안 새 배포는 release 의 진행 중 unique index 가 막는다 |
| 6 | 저장소 이름은 `source_repository_url` 에서 구하고, GitHub 설치 ID 는 `github_installations.installation_id` 에서 읽고, 설치 토큰 범위는 저장소 숫자 ID 대신 이름으로 좁힌다 | `develop` 의 `services.github_installation_id` 는 GitHub 의 ID 가 아니라 내부 테이블의 FK 이고, 저장소 숫자 ID 컬럼은 없다 |
| 7 | 서비스 도메인을 `{slug}.…` 에서 `{이름을 DNS label 로 변환}-{service_id}.…` 로 바꿨다 | `develop` 에 `slug` 컬럼이 없고 이름은 프로젝트 안에서만 유일하다. chart 의 `route.host` 검증(소문자·숫자·하이픈, 63자 이하)을 지키려고 #17 에서 변환을 넣었다 |
| 8 | Release 를 타깃마다 만든다. 서비스·타깃마다 진행 중 release 는 하나다 | ADR 0004·용어 사전. 타깃은 서비스에 연결된 AWS 타깃이고 없으면 시드된 `aws` 다. AWS 타깃 하나만 지원한다 |
| 9 | 서비스의 `builder` 가 비어 있으면 소스를 보고 정한다. `iris.json` 에 없는 빌드·시작 명령은 서비스에 저장된 값(코드 분석 결과)을 쓴다 | 현겸님의 우선순위(`iris.json` > 서비스 설정 > 자동 감지)를 유지하고, `Builder.AUTO` 대신 `None` 을 자동 감지로 본다. 용어 사전은 "빌더가 비어 있으면 배포하지 않는다"고 해서 **차이가 있다** |
| 10 | `GITHUB_PUBLIC_INSTALLATION_ID` 설정을 앱에서 뺐다 | `develop` 의 서비스는 항상 GitHub 설치에 연결된다(NOT NULL). 앱을 설치하지 않은 공개 저장소를 빌드하는 경로가 없어졌다. infra chart 는 이 키를 계속 주입하지만 앱이 무시한다 |
| 11 | Worker 통합 테스트: 스키마를 지우던 방식(`drop_all`/`create_all`)을 데이터만 비우는 방식으로 바꾸고 `integration` 마커를 달았다 | 기존 방식은 마이그레이션이 만든 스키마와 시드 데이터를 파괴하고, 마커가 없어 CI `database` 잡에서 돌지 않았다. 시나리오 하나(`release in flight snooze`)는 `develop` 의 "서비스당 진행 중 요청 하나" 제약 때문에 첫 요청을 `FAILED`(롤백 대기)로 만들어 재현한다 |

## 마이그레이션과 DB 초기화

### 옛 revision 을 지운 이유
`main` 의 `cf3b3859224c`·`499d60073fc0` 과 `develop` 의 `da3066d208c9` 이후 체인은 모두 `down_revision=None` 에서 시작해 같은 이름의 테이블을 만든다. 한 DB 에 둘 다 적용할 수 없고 한 줄로 이을 수도 없다. 그래서 `develop` 체인을 기준으로 하고, 그 위에 revision `54eb3cd02e14` 하나로 Build·Release 컬럼과 enum CHECK 제약 변경을 추가했다. `alembic heads` 는 1개다.

**규칙 예외:** `db-migration.md` 는 "main 에 머지됐거나 어느 환경에든 적용된 revision 은 수정하지 않는다"고 한다. 이번에는 revision 을 수정한 것이 아니라 체인에서 제거했고, 그 revision 이 적용된 DB 를 같은 이유로 비웠다.

### DB 를 비운 이유 (확인한 근거)
1. GitOps 저장소에 `api` digest 가 **2026-10-02T09:14:50Z** 에 커밋돼 있다(커밋 `abab02a`, "deploy platform api: iris-was edcbda4"). iris-infra 의 platform chart 는 `api.digest` 가 있으면 Argo Sync hook 으로 `alembic upgrade head` 를 실행한다.
2. 읽기 전용으로 조회한 결과 `softbank_iris` 에는 `alembic_version = 499d60073fc0` 과 테이블 7개(`alembic_version`, `builds`, `deployment_requests`, `jobs`, `releases`, `services`, `users`)가 있었고, 데이터 테이블 6개는 **모두 0건**이었다. `iris`·`postgres` DB 는 테이블이 없었다.
3. 새 이미지에는 `499d60073fc0` revision 파일이 없다. DB 가 그 버전을 가리키면 `alembic upgrade head` 가 알 수 없는 revision 이라며 실패하고 migration Job 이 막혀 Argo sync 가 진행되지 않는다(iris-infra runbook 의 "이전 이미지에 현재 DB revision 파일이 없으면 migration 이 실패한다"와 같은 원리).
4. 대안과 탈락 이유
   - 옛 스키마를 새 스키마로 바꾸는 변환 revision: 보존할 데이터가 0건인데 컬럼 이름 변경·새 NOT NULL 컬럼 처리와 테스트가 필요하다.
   - `alembic_version` 만 고치는 stamp: 테이블 모양이 옛 구조라 새 모델과 맞지 않는다.

### 실행 기록
PR #14 머지 직후 같은 날 수행했다. `iris` AWS 프로필로 SSM bridge 터널을 열고 읽기 전용 세션으로 조회한 뒤, 쓰기 세션에서 한 트랜잭션 안에서 "예상 테이블 7개 · 버전 `499d60073fc0` · 데이터 테이블 0건"을 다시 확인하고 하나라도 다르면 중단하도록 했다. 통과했을 때만 `DROP TABLE … CASCADE` 를 실행했고, 이후 테이블·시퀀스·인덱스·타입이 남지 않았음을 확인했다. **DB 와 `public` 스키마는 남겼고**, 데이터가 0건이어서 백업·스냅샷은 만들지 않았다(runbook 은 배포 전 RDS 백업 확인을 권한다. 이의가 있으면 알려 달라).

다음 `Deploy platform` 의 migration 이 `54eb3cd02e14` 까지 처음부터 만든다. 배포 전에 옛 이미지로 migration 이 다시 돌면 옛 스키마가 되살아날 수 있으므로, 그때는 `alembic_version` 이 `499d60073fc0` 인지 보고 다시 비운다.

## 검증
- CI `check`·`database`(마이그레이션 왕복·`alembic check`·통합 테스트)가 PR #14·#15·#16·#17 에서 통과했다.
- 로컬에서 RDS 와 같은 메이저인 PostgreSQL 17 로 `upgrade head` → `alembic check` → `downgrade base` → `upgrade head` 를 확인했고 테스트 402개가 통과했다(통합 포함).
- **하지 못한 것:** 실제 AWS·GitHub·Argo 호출(테스트는 대역), 클러스터 Secret 존재 확인, k8s 안의 `DATABASE_URL` 확인.

## 결과
- 좋아지는 점: 한 코드·한 스키마에서 API → jobs → Worker → GitOps 가 이어지고, 상태 이력이 보장되며, Worker 통합 테스트가 CI 에서 돈다.
- 감수하는 점: 현겸님의 Worker 코드가 바뀌었다(위 표). `SUPERSEDED` 가 ADR 0010 과 어긋난다. 서비스당 AWS 타깃 하나만 지원한다. 공개 저장소를 앱 없이 빌드하는 경로가 없다.

## 현겸님께 확인 요청
1. 위 표 1~11 중 의도와 다르게 바뀐 곳이 없는지. 특히 롤백 흐름(5)과 취소(4)
2. `softbank_iris` 에서 남겨야 할 것이 없었는지, 그리고 서버가 실제로 쓰는 DB 이름. 시크릿 `iris-dev-platform-db` 의 `dbname` 은 `iris` 인데 마이그레이션이 적용된 DB 는 `softbank_iris` 였다
3. 옛 revision 제거(규칙 예외)에 동의하는지
4. 빌더 설정 파일 이름: `iris.json`(현겸님) vs `.anydeploy/build.yaml`(용어 사전)
5. Worker 용 Secret(`iris-build-github-app`, `iris-gitops-github-app`, `iris-argocd-reader`) 준비 여부
6. 앞으로는 `develop` 에서 브랜치를 따서 `develop` 으로 PR 을 올리는 것

## 후속
- 서비스 이름 규칙: ADR 0006 은 이름이 DNS 레이블 규칙(소문자·숫자·하이픈, 63자)이라고 하지만 API 는 길이(1~63)만 검사한다. #17 은 Worker 쪽 방어이고 API 검증은 별도 과제다.
- iris-infra chart 의 `GITHUB_PUBLIC_INSTALLATION_ID` 주입 정리
- 다중 타깃(타깃별 GitOps 경로)

## 보강
- ADR 0010 의 허용 표에 `SUPERSEDED`(진행 중 상태에서만 이동, 끝 상태)가 추가됐다. 용어 사전 §5 에 반영했다.
