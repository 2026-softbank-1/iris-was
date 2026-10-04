# 설정 (환경변수 · GitHub App)

README 에서 옮긴 상세다. 로컬 실행 순서는 [README](../README.md#빠른-시작)를 본다.

## 로컬 개발 환경

```bash
uv sync

# 로컬 DB: Docker(OrbStack)로 PostgreSQL 18 을 띄우고 .env 의 DATABASE_URL 을 맞춘다
scripts/dev-db.sh                  # 켜기 (stop: 끄기, reset: 데이터까지 삭제)

cp -n .env.example .env            # .env 가 없을 때만. DB 외 값(GitHub App 등)을 채운다
uv run alembic upgrade head
uv run alembic current             # 접속·적용 revision 확인
```

- DB 는 `docker-compose.dev.yml`(`restart: unless-stopped`, named volume)로 상시 떠 있고 `127.0.0.1:5432` 에서만 접속된다. 비밀번호는 `scripts/dev-db.sh` 가 만들어 `.env` 에만 둔다.
- 직접 만든 PostgreSQL 을 쓰려면 스크립트 없이 `DATABASE_URL` 만 `.env` 에 넣어도 된다(DB 이름은 `softbank_iris` 로 통일).

`.env` 예:

```dotenv
DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@localhost:5432/softbank_iris
LOG_LEVEL=INFO
```

- `DATABASE_URL` 이 없으면 Control API·Worker 는 시작하자마자 설정 오류로 종료한다.
- `pytest` 는 DB 없이 돈다(`tests/conftest.py` 가 접속되지 않는 URL 을 넣는다). 실제 DB 연결은 서버를 띄워 `GET /readyz` 가 204 인지로 확인한다.
- Repository 통합 테스트(`@pytest.mark.integration`)는 `alembic upgrade head` 가 끝난 DB 를 `TEST_DATABASE_URL` 로 주면 실행되고, 없으면 건너뛴다. 테스트는 트랜잭션을 롤백한다.

## Control API

| 환경변수 | 설명 |
|---|---|
| `DATABASE_URL` | PostgreSQL 접속 URL. `postgresql+asyncpg://<USER>:<PASSWORD>@<HOST>:5432/<DB>` |
| `LOKI_URL`, `PROMETHEUS_URL` | 로그·메트릭 백엔드 내부 주소. 없으면 관측 API 503. [로그·메트릭 연결 및 API](observability-api.md) |
| `TRAFFIC_CLUSTER` | 서비스 외부 트래픽 지표(요청 수·오류율·응답 시간·공용 네트워크)의 `cluster` 라벨 값. 기본 `iris-dev-workload` |
| `DIAGNOSIS_AGENT_URL`, `DIAGNOSIS_AGENT_API_KEY` | 에러 진단 에이전트 서버(`iris-error-check-agent`) 주소와 `X-API-Key` 값. dev 클러스터 주소는 `http://iris-platform-error-agent.iris-platform.svc.cluster.local:8001`, 키는 Secret `iris-error-agent` 의 `AGENT_API_KEY` 와 같은 값. 둘 중 하나라도 없으면 진단 시작이 `503 NOT_CONFIGURED`(저장된 진단 조회는 가능). [AI 진단 API](diagnosis-api.md) |
| `DIAGNOSIS_AGENT_TIMEOUT_SECONDS` | 에이전트 응답을 기다리는 시간(초). 기본 150 (모델 호출 최대 2번 × 60초 + 여유) |
| `REPAIR_AGENT_URL`, `REPAIR_AGENT_API_KEY`, `REPAIR_AGENT_SOURCE_HOSTS` | 수정 후보 에이전트 주소·인증 키·허용할 source snapshot 호스트. 특정 진단 원문과 고정 소스를 보내고 결과·검토용 파일을 저장한다. [연동 계약](repair-agent-integration.md) |
| `REPAIR_AGENT_TIMEOUT_SECONDS`, `REPAIR_AGENT_DEADLINE_SECONDS`, `REPAIR_AGENT_MAX_COST_USD` | 호출 대기 150초·작업 기한 240초·후보 생성 비용 상한 USD 1. 응답이 불확실하면 결과만 조회하며 모델을 자동 재호출하지 않는다 |
| `DIAGNOSIS_AUTO_START_ENABLED`, `DIAGNOSIS_AUTO_START_INTERVAL_SECONDS` | 실패가 확정된 배포를 서버가 자동으로 진단한다(기본 켬, 에이전트 설정이 없으면 켜지 않는다). 모델 비용이 실패마다 들어 `false` 로 끌 수 있다(끄면 버튼으로 시작하는 진단만 남는다). 진단할 배포를 찾는 주기는 기본 5초다 |
| `AWS_REGION`, `ARTIFACT_BUCKET` | (선택) 둘 다 있어야 켜진다(`AWS_REGION` 은 아래 `BUILD_LOG_GROUP` 도 함께 쓴다). ① 소스 업로드 API(`likelion up`): `uploads/*` 의 `s3:PutObject`·`s3:AbortMultipartUpload` 가 필요하고, 없으면 `POST /services/{id}/uploads` 가 `503 NOT_CONFIGURED`. ② 진단에 빌드의 소스 스냅샷을 함께 보낸다: `snapshots/*` 의 `s3:GetObject` 가 필요하고, 없으면 로그만 진단한다. 같은 두 변수가 둘을 함께 켜므로 Role 에 두 권한을 같이 주고, **권한을 먼저 적용한 뒤** 변수를 켠다. 운영은 chart 값이 아니라 Secret `iris-platform-was-env` 에 `ARTIFACT_BUCKET` 을 넣고 API 를 롤링 재시작한다([ADR 0023](adr/0023-cli-source-upload-storage-and-archive-defense.md)) |
| `DEPLOYMENT_STRATEGY_ENABLED` | 카나리·블루그린 배포 방식(기본 `false`). 꺼져 있으면 두 방식 저장이 `422` 이고 새 배포 요청은 `ROLLING` 으로 적용한다. Deploy Worker 와 같은 값으로, `iris-service` chart 0.7.0 이 배포된 뒤에 켠다. [ADR 0028](adr/0028-deployment-strategy-selection.md) |
| `PROJECT_NETWORKING_ENABLED` | 관리형 DB·호스트 별칭·참조 변수(프로젝트 내부 통신, 기본 `false`). 꺼져 있으면 DB 생성·별칭 저장·참조 변수가 `422`(reason `project_networking_disabled`)이고 apply 는 DB 를 만들지 않는다. Deploy Worker 와 같은 값으로, **iris-infra AWS ApplicationSet 의 `iris-service` chart pin 이 0.9.0 이 된 뒤에** 켠다. [ADR 0031](adr/0031-project-stacks-databases-and-variable-references.md) |
| `DATABASE_IMAGES` | (선택) 관리형 DB 고정 이미지 바꾸기. JSON `{"postgres": "docker.io/library/postgres:16-alpine@sha256:…"}`. 엔진 공식 리포지토리·digest 만 받는다. 비우면 코드 기본값(2026-10-04 고정 digest: postgres:16-alpine·mysql:8.4·mongo:7·redis:7-alpine) |
| `ONPREM_TAILSCALE_AUTH_KEY` | (선택) 사용자 온프레미스 서버가 tailnet 에 가입하는 Tailscale 키(reusable·pre-approved·`tag:iris-onprem`). 없으면 `POST /onprem-servers/bootstrap` 이 `503 NOT_CONFIGURED`. 화면·CLI·로그에 내보내지 않는다. [ADR 0029](adr/0029-user-registered-onprem-servers.md) |
| `ONPREM_ECR_PULL_ROLE_ARN`, `ONPREM_ECR_PULL_SESSION_SECONDS` | (선택) `AWS_REGION` 과 함께 있으면 서버에 ECR pull 자격증명을 준다(`POST /onprem-servers/registry-credentials`). Control API Role 에 이 Role 의 `sts:AssumeRole` 이 있어야 한다. Control API 자격증명이 이미 role 세션이라 AssumeRole 이 연쇄되어 세션 길이는 1시간까지다(기본 3600초, 서버 CronJob 이 5분마다 갱신한다). 없으면 `503 NOT_CONFIGURED`. 서버가 아직 `CONNECTED` 가 아니면 `409 ONPREM_SERVER_NOT_CONNECTED` |
| `ONPREM_K3S_VERSION`, `ONPREM_ARGO_ROLLOUTS_VERSION`, `ONPREM_SEALED_SECRETS_VERSION` | 설치 스크립트가 서버에 고정해 설치하는 버전. 기본 `v1.33.13+k3s2`·`v1.10.0`·`0.40.0`(iris-infra 가 고정한 버전과 같이 올린다) |
| `UPLOAD_MAX_BYTES` | 소스 업로드의 압축한 바이트 한도. 기본 250MB(Build Worker 의 `SNAPSHOT_MAX_BYTES` 와 같게 둔다). 넘으면 본문을 읽기 전에 `413 UPLOAD_TOO_LARGE` |
| `BUILD_LOG_GROUP` | (선택) `AWS_REGION` 과 함께 있으면 배포 상세의 빌드 로그 전체를 CloudWatch Logs 에서 읽는다(그룹 `/aws/codebuild/iris-dev-build` 의 `logs:GetLogEvents` 만 허용한 Role 필요). 없으면 Build Worker 가 남긴 실패한 빌드의 끝부분만 보여 주고, 그것도 없으면 빌드 로그 API 가 503 이다. [배포 상세 화면 API](deployment-details-api.md) |
| `LOG_LEVEL` | `DEBUG`·`INFO`·`WARNING`·`ERROR`. 기본 `INFO` |
| `WEB_BASE_URL` | 웹 프런트 주소. 로그인 후 이 주소로 돌려보낸다. 기본 `http://localhost:3000` |
| `API_BASE_URL` | Control API 의 공개 주소(예: `https://api.likelion.uk`). CLI 로그인의 `verificationUrl` 과 온프레미스 서버의 `installCommand` 를 만든다. CLI 로그인은 없으면 요청의 Host 로 만들지만, 온프레미스 서버 등록·토큰 재발급은 없거나 https 가 아니면(localhost·127.0.0.1 의 http 는 허용) `503 NOT_CONFIGURED` 다. TLS 를 앞단에서 끝내는 운영에서는 꼭 설정한다 |
| `CORS_ALLOW_ORIGIN_REGEX` | CORS 허용 Origin 정규식(전체 일치). 기본은 `likelion.uk`·하위 도메인(https)과 `localhost`·`127.0.0.1` 모든 포트. 메서드·헤더는 전부 허용하고 쿠키(credentials)도 허용한다 |
| `SESSION_SECRET` | 세션·OAuth state 서명 키(HS256). 없으면 로그인·인증 API 가 `503 NOT_CONFIGURED` |
| `SESSION_TTL_MINUTES` | 세션 유효 시간(분). 기본 7일 |
| `IS_SESSION_COOKIE_SECURE` | 쿠키 Secure 속성. 기본 `true`, http 로컬 개발에서는 `false` |
| `GITHUB_APP_ID` · `GITHUB_APP_SLUG` | GitHub App ID, 설치 페이지 주소에 쓰는 slug |
| `GITHUB_APP_CLIENT_ID` · `GITHUB_APP_CLIENT_SECRET` | 로그인(사용자 인증)용. 없으면 `503 NOT_CONFIGURED` |
| `GITHUB_APP_PRIVATE_KEY` | App JWT 서명용 PEM. 줄바꿈은 `\n` 도 허용. 없으면 저장소·서비스 API 가 `503 NOT_CONFIGURED` |
| `GITHUB_WEBHOOK_SECRET` | 웹훅 서명 검증 키(App 설정의 Webhook secret 과 같은 값). 없으면 웹훅 API 가 `503 NOT_CONFIGURED` |
| `VARIABLES_ENCRYPTION_KEY` | 서비스 환경변수 값을 DB 에 암호화해 저장하는 Fernet 키. `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 로 만든다. 없으면 변수 API 가 `503 NOT_CONFIGURED`. 잃으면 저장된 값을 읽을 수 없다 |

## Build Worker

Build Worker 만 쓰는 값(`BuildWorkerSettings`). Control API 에는 넣지 않는다.

| 환경변수 | 설명 |
|---|---|
| `GITHUB_APP_ID` · `GITHUB_APP_PRIVATE_KEY` | Iris GitHub App ID·private key(PEM). 운영은 Secrets Manager → K8s Secret 으로 주입 |
| `AWS_REGION` | CodeBuild·ECR·S3 리전 |
| `CODEBUILD_PROJECT` · `ARTIFACT_BUCKET` | iris-infra `aws/dev/foundation` 출력값. dev: `iris-dev-build` · `iris-dev-build-artifacts-<ACCOUNT_ID>-ap-northeast-2` |
| `CONCURRENCY` | Worker 1개가 동시에 처리할 BUILD job 수. 기본 4 |
| `USER_CONCURRENT_BUILD_LIMIT` · `BUILD_TIMEOUT_MINUTES` · `SNAPSHOT_MAX_BYTES` | 사용자별 동시 빌드 2 · 빌드 15분 · 스냅샷 250MB |
| `UPLOAD_MAX_UNCOMPRESSED_BYTES` · `UPLOAD_MAX_ENTRIES` | `CLI` 업로드 아카이브를 풀었을 때의 총 크기 2GiB · 항목 수 10만(압축 폭탄 방어). 넘으면 `SOURCE_TOO_LARGE`. 레포 구성 분석이 GitHub tarball 을 풀 때도 같은 한도를 쓴다 |
| `ANALYSIS_GATE_COMMAND` | 레포 구성 분석기 명령. JSON 배열(argv)로 적는다(셸을 거치지 않는다). 기본 `["<python>", "-m", "iris_analyzer.gate.cli", "--request-stdin"]`(이미지에 설치된 vendored wheel). 분석기에는 `PATH`·`LANG` 외 환경변수를 넘기지 않는다([ADR 0030](adr/0030-repository-analysis-gate.md)) |
| `ANALYSIS_GATE_TIMEOUT_SECONDS` · `ANALYSIS_GATE_CONCURRENCY` | 분석 1건 제한 시간 120초(0 초과 600 이하, 넘으면 `ANALYZER_TIMED_OUT`) · Worker 1개가 동시에 실행하는 분석 수 2(빌드 슬롯과 따로 센다) |

분석기 wheel 갱신: iris-code-analyzer-agent 의 고정 커밋에서 `python -m build --wheel` 로 만든 wheel 을 `vendor/` 에 두고 `vendor/iris-analyzer-manifest.json`(sourceCommit·sha256)과 `pyproject.toml` 의 `[tool.uv.sources]` 경로를 맞춘 뒤 `uv lock` 한다. 버전이 같아도 `uv.lock` 이 wheel sha256 을 고정하므로 내용이 바뀌면 lock 도 바뀐다. 이미지(`Dockerfile`)는 `vendor/` 를 복사해 같이 설치한다.

Build Worker 역할(IAM)에는 `CLI` 업로드를 내려받는 `uploads/*` 의 `s3:GetObject` 가 있어야 한다(없으면 `CLI` 빌드가 `BUILD_INFRA_ERROR` 로 끝난다. 인라인 정책 변경은 떠 있는 Pod 에도 바로 적용되므로 Worker 를 재시작하지 않는다). 또 실패한 빌드의 CloudWatch 로그를 읽는 `logs:GetLogEvents`(`/aws/codebuild/<프로젝트>:*`)가 있어야 한다. 없어도 빌드는 동작하고 AI 진단만 빌드 로그 없이 끝난다([ADR 0020](adr/0020-ai-error-diagnosis-via-agent-server.md)).

## Deploy Worker

Deploy Worker 만 쓰는 값(`DeployWorkerSettings`). Build Worker 와 GitHub App·자격증명을 공유하지 않는다.

| 환경변수 | 설명 |
|---|---|
| `AWS_REGION` | ECR 리전 (`r-*` 태그) |
| `GITOPS_REPOSITORY` | `{owner}/gitops-environments`. `main` 에 fast-forward 커밋만 한다 |
| `GITOPS_APP_ID` · `GITOPS_APP_PRIVATE_KEY` · `GITOPS_INSTALLATION_ID` | `iris-gitops` GitHub App(contents:write, GitOps 저장소에만 설치) |
| `ARGOCD_SERVER_URL` · `ARGOCD_TOKEN` | Argo CD API 주소, project role `deploy-reader` 토큰(applications get) |
| `VARIABLES_ENCRYPTION_KEY` | Control API 와 같은 값. 배포 요청의 변수 스냅샷(암호문)을 풀 때 쓴다. 변수가 있는 배포에만 필요하고, 변수가 있는데 없으면 그 배포는 `DEPLOY_INFRA_ERROR` 로 실패한다 |
| `SEALED_SECRETS_CERT` | workload 의 Sealed Secrets controller 공개 인증서(PEM, 비밀이 아니다. `\n` 두 글자로 적어도 된다). [runbook](https://github.com/2026-softbank-1/iris-infra/blob/main/docs/runbooks/sealed-secrets.md) 에서 꺼낸다. **설정하면 사용자 변수 기능이 켜져** values 에 `iris`·`variables` 를 쓴다. `iris-service` chart 0.6.0 이상이 배포된 뒤에만 설정한다(이전 chart 는 모르는 키를 거절해 모든 배포가 실패한다). 비어 있으면 이전과 같은 values 를 쓴다. 운영은 Secret `iris-platform-was-env` 에 있다(2026-10-03 설정) |
| `ARGOCD_PROBE_TOKEN` | 등록한 서버의 probe Application(Argo project `iris-onprem-probe`)을 읽는 토큰(role `iris-deploy-reader`, applications get). `ARGOCD_TOKEN` 은 자기 project 만 보므로 따로 받는다. 없으면 서버 연결 확인을 하지 않아 서버가 `REGISTERING` 에 머문다(경고 로그, 기한 초과로 실패시키지 않는다) |
| `PLATFORM_SEALED_SECRETS_CERT` | management 클러스터 Sealed Secrets controller 공개 인증서(PEM). 사용자가 등록한 온프레미스 서버의 Argo CD cluster 접속 정보를 `argocd/cluster-onprem-{key}` 용으로 봉인해 `platform/onprem-servers/{key}/values.yaml` 에 커밋한다. 없으면 서버 동기화를 하지 않아 서버가 `REGISTERING` 에 머문다. `SEALED_SECRETS_CERT`(workload)와 다른 인증서다. 서버로 가는 서비스 변수는 서버가 보낸 인증서로 봉인하고, 서버 타깃 values 에는 `imagePullSecrets: [{name: iris-ecr-pull}]` 를 더한다(iris-service chart 0.8.0). [ADR 0029](adr/0029-user-registered-onprem-servers.md) |
| `DEPLOYMENT_STRATEGY_ENABLED` | 켜면 AWS 타깃 release 의 values 에 배포 요청의 적용 방식 `deploymentStrategy` 를 쓴다(기본 `false`). on-prem 타깃은 chart 0.6.0 에 남아 켜도 쓰지 않는다. `iris-service` chart 0.7.0(Argo Rollouts) 이상이 배포된 뒤에만 켠다(이전 chart 는 모르는 키를 거절해 모든 배포가 실패한다). Control API 와 같은 값으로 둔다 |
| `PROJECT_NETWORKING_ENABLED` | 켜면 AWS 타깃 release 의 values 에 chart 0.9.0 키를 쓴다: 앱은 `projectId`·`service.exposeContainerPort: true`·`hostAliases`(있을 때)·스택 앱의 `containerPort`(분석 포트), 관리형 DB 는 `workload.kind: database`·`database.{engine,image,storage,port}`·`projectId`(빌드 이미지·command 없음). 참조 변수는 봉인 직전에 대상 서비스의 지금 값(DB 자격 증명)으로 푼다. 기본 `false` 면 values 가 이전과 같다. **AWS chart pin 0.9.0 이 반영된 뒤에** 켠다(0.8.x 이하 schema 는 모르는 키를 거절해 모든 배포가 실패한다). 켠 뒤 첫 배포에서 기존 앱도 `projectId` 라벨로 Pod 가 한 번 다시 뜬다. Control API 와 같은 값으로 둔다 |

## GitHub App

로그인과 저장소 접근을 GitHub App 하나로 처리한다. 사용자 토큰은 로그인 때 한 번만 쓰고 저장하지 않으며, 저장소 접근은 설치(installation) 토큰으로 한다.

App 설정에서 맞춰야 할 값:

- Callback URL: `<API 주소>/api/v1/auth/github/callback` (웹 로그인과 CLI 로그인 승인이 같은 콜백을 쓴다)
- **Request user authorization (OAuth) during installation** 켜기 (설치 직후 로그인으로 이어진다)
- 권한: Repository → Contents `Read-only`, Metadata `Read-only`
- 웹훅(push 자동 배포·설치 동기화): Webhook URL `<API 주소>/api/v1/webhooks/github`, Content type `application/json`, Secret 은 `GITHUB_WEBHOOK_SECRET` 과 같게, 이벤트는 Push 를 구독한다. 설계는 [ADR 0009](adr/0009-github-webhook-receiver.md).
- 로컬에서 웹훅을 받으려면 터널로 `localhost:8000` 을 노출한다(예: `npx smee-client --url <smee 채널> --target http://localhost:8000/api/v1/webhooks/github`). 개발용 App 에서만 켠다.

