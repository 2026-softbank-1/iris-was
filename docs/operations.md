# 운영 (실행 · DB 마이그레이션 · 배포 · 자주 쓰는 명령)

README 에서 옮긴 상세다.

## 실행

```bash
uv run uvicorn app.main:app --reload          # Control API (GET /healthz: 생존, GET /readyz: DB 연결 — 정상 204, 실패 503)
uv run python -m app.workers.build_worker     # Build Worker
uv run python -m app.workers.deploy_worker    # Deploy Worker
```

Worker 는 일이 없으면 `jobs` 트리거의 `NOTIFY jobs, <kind>`·가장 이른 미래 `run_after`·60초 중 먼저 오는 때까지 기다렸다가 선점을 다시 시도한다(ADR 0019). SIGTERM·SIGINT 를 받으면 루프를 끝내고 종료한다. Build Worker 는 CodeBuild 를 기다리던 job 을 반납하고, 다른 Worker 가 기록된 `codebuild_build_id` 로 이어서 처리한다. 스냅샷(최대 250MB 다운로드·업로드) 중에는 반납하지 않으므로 Pod `terminationGracePeriodSeconds` 를 120 이상으로 둔다.

Build Worker 흐름: BUILD job 선점 → GitHub tarball(S3 스냅샷. `CLI` 요청은 올린 아카이브를 받아 검사하며 같은 모양으로 다시 묶는다, [ADR 0023](adr/0023-cli-source-upload-storage-and-archive-defense.md)) → 빌더 결정(`iris.json` > 서비스 설정 > Dockerfile 유무) → CodeBuild(buildspec 은 iris-infra `terraform/environments/aws/dev/foundation/buildspec.yml`. 환경변수 이름이 계약이다) → ECR digest 조회 → 같은 트랜잭션에서 `builds=SUCCEEDED`·요청 `DEPLOYING`·DEPLOY job 생성.

Deploy Worker 흐름: DEPLOY 선점 → release(PENDING) 생성(서비스·타깃마다 1개, 진행 중이면 snooze) → `services/{service_id}/prod/values.yaml`(플랫폼 Helm chart values) 렌더링(`SEALED_SECRETS_CERT` 가 있으면: 요청의 변수 스냅샷은 풀어 `svc-{service_id}` + `vars-r{release_id}` 용으로 다시 봉인해 `variables` 에 넣고, 서비스·타깃 이름과 배포 요청 id 는 `iris` 에 넣는다. [ADR 0017](adr/0017-service-variables-encrypted-storage-and-deploy-snapshot.md)) → 커밋 SHA 기록 → `main` fast-forward → RECONCILE 이 10초마다 Argo CD 상태 확인(대기 중에는 job 을 잡지 않고 snooze) → 성공이면 ECR `r-{release_id}` 태그·`SUCCEEDED`, 실패면 이전 정상 release 의 디렉터리로 되돌리는 revert commit(ROLLBACK). 상세는 [.claude/docs/deploy-worker-plan.md](../.claude/docs/deploy-worker-plan.md), 로컬 테스트는 [docs/deploy-worker-test-guide.md](deploy-worker-test-guide.md).

로그는 stdout 에 JSON 한 줄씩 나간다. 로컬에서는 `... | jq` 로 보면 편하다. 로깅·응답·예외 구조는 [docs/api-response-logging-template.md](api-response-logging-template.md) 참조.

## DB 마이그레이션

Control API·Worker 는 시작할 때 마이그레이션을 실행하지 않는다. 여러 replica 가 동시에 뜨면 마이그레이션도 동시에 돌아 충돌하므로, 항상 별도로 한 번만 실행한다.

```bash
# 로컬
uv run alembic upgrade head                 # 최신까지 적용
uv run alembic downgrade -1                 # 한 단계 되돌리기
uv run alembic current                      # 현재 적용된 revision
uv run alembic upgrade head --sql           # DB 없이 실행될 SQL 만 출력

# 컨테이너 (API·Worker 와 같은 이미지)
docker build -t was .
docker run --rm -e DATABASE_URL=<DATABASE_URL> was alembic upgrade head
```

운영에서는 Argo CD **PreSync hook Job** 이 앱 배포 직전에 같은 이미지 digest 로 `alembic upgrade head` 를 한 번 실행한다. 이 Job 은 DDL 권한 DB 계정을 쓰고, API·Worker 는 DML 권한 계정만 쓴다.

```yaml
metadata:
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: <ECR_REPO>@sha256:<DIGEST>
          command: ["alembic", "upgrade", "head"]
```

새 revision 은 로컬 DB 에서 아래 순서로 검증한 뒤 커밋한다.

```bash
uv run alembic revision --autogenerate -m "..."   # 생성 후 파일을 열어 검토
uv run alembic upgrade head
uv run alembic check                              # "No new upgrade operations detected" 여야 한다
uv run alembic downgrade -1 && uv run alembic upgrade head
```

마이그레이션 작성 규칙은 [.claude/rules/db-migration.md](../.claude/rules/db-migration.md) 를 따른다.

## 배포 (management EKS)

GitHub Actions **Deploy platform**(`workflow_dispatch`, main 전용)으로만 배포한다.

1. 실행 시 `api`(Control API, DB migration 포함)·`build_worker`·`deploy_worker`·`console_gateway`(서비스 콘솔 셸 중계, ADR 0033) 중 배포할 것을 고른다. `console_gateway` 는 기본 꺼져 있다.
2. 이미지를 한 번 빌드해 ECR `iris/was`에 push 하고, 고른 컴포넌트의 digest 만 `iris-gitops-environments`의 이 레포 전용 파일 `platform/aws-dev-management/was.yaml`(`api`·`buildWorker`·`deployWorker`·`consoleGateway` 의 `digest`)에 커밋한다.
3. management EKS의 Argo CD(`iris-platform`)가 반영한다. DB 스키마 변경은 api 배포의 migration 으로만 적용된다.

GitHub 설정은 secret `GITOPS_APP_PRIVATE_KEY`(GitHub App `softbank-iris-github-app`, ID `5148916`) 하나다. 계정·ECR 역할(`iris-dev-github-ecr-was`)·저장소 이름은 workflow 상수다.

Secret·RDS·rollback 절차는 iris-infra `docs/runbooks/deploy-platform.md`를 본다.

## 자주 쓰는 명령

```bash
uv run ruff check . && uv run ruff format --check .   # 린트·포맷
uv run mypy app                                       # 타입 검사
uv run pytest                                         # 테스트
TEST_DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@localhost:5432/softbank_iris_test uv run pytest  # 통합 테스트 포함. `alembic upgrade head` 를 끝낸 전용 DB 여야 한다(데이터 테이블을 비운다)
uv run alembic revision --autogenerate -m "..."       # 마이그레이션 생성
```
