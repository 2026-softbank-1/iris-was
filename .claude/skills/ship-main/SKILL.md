---
name: ship-main
description: Use when 이 프로젝트(WAS)의 검증된 변경을 main 에 릴리스하고 management EKS 에 배포할 때 — "main 릴리스", "운영 배포", "ship-main". release 브랜치 준비 → CI → main 병합(명시 승인 필요) → Deploy platform → 검증 → main 을 develop 에 되돌려 합치기까지 다룬다.
allowed-tools: "Bash, Read, Grep, Glob, Write, Edit"
---

# main 릴리스·배포 스킬 (WAS)

`iris-was`(`2026-softbank-1/iris-was`)의 변경을 `main` 에 올리고 배포한다. 근거 문서: [ADR 0008](../../../docs/adr/0008-branch-strategy.md)(브랜치 전략), [git-conventions](../../rules/git-conventions.md), README §배포·§DB 마이그레이션, `.github/workflows/deploy-platform.yml`.

## 대상과 배포 경로

| 항목 | 값 |
|---|---|
| 저장소·기준 브랜치 | `2026-softbank-1/iris-was`, 릴리스 대상 `main` |
| 병합 방식 | release → main PR 은 **merge commit** (git-conventions §3) |
| 필수 CI | `check`(ruff·format·mypy·pytest), `database`(alembic head 1개·upgrade·check·통합 테스트·왕복) |
| 배포 | GitHub Actions **Deploy platform**(`workflow_dispatch`, **main 에서만**) |
| 배포 결과 | 이미지를 ECR 에 push → GitOps `platform/aws-dev-management/was.yaml` 의 `api`·`buildWorker`·`deployWorker` digest 커밋 → management EKS 의 Argo CD(`iris-platform`)가 반영 |
| DB 마이그레이션 | `api` 배포 때 Argo PreSync Job 이 같은 digest 로 `alembic upgrade head` 를 한 번 실행한다. 앱은 시작 때 마이그레이션하지 않는다 |
| 운영 주소 | `https://api.likelion.uk` (`/healthz`·`/readyz` 는 204) |

## 승인 경계

- **main 병합은 사용자의 명시 지시가 있어야 한다.** "릴리스 준비"·`ship-main` 호출만으로는 병합하지 않는다. 같은 릴리스에 이미 받은 명시 승인은 다시 묻지 않는다. 승인 범위는 요청에 적힌 변경과 `api`·Worker 중 요청한 컴포넌트까지다.
- main 에 직접 push, force push, 보호 규칙 우회(관리자 병합)는 하지 않는다.
- 이 레포는 **버전 태그·GitHub Release·Linear 를 쓰지 않는다**(사용자 지시 2026-10-02). 전역 정책의 태그·릴리스 노트·Linear 단계는 건너뛴다. 병합·배포·운영 검증·되돌려 합치기까지만 한다.
- 운영 DB 를 직접 조회·변경하지 않는다(필요하면 사용자에게 먼저 묻는다). 마이그레이션은 위 PreSync Job 만 적용한다.

## 절차

### 1. 범위 고정
```bash
git fetch origin --prune
git rev-parse origin/main                    # 릴리스 기준 SHA 로 기록
git log --oneline origin/main..origin/develop # main 에 없는 develop 커밋
```
- develop 에는 다른 작업·미검증 변경이 섞일 수 있다. **기본은 요청한 작업의 squash 커밋만** 릴리스한다. 커밋별로 diff 를 보고 그 작업의 변경인지, 다른 작업을 필요로 하지 않는지(import·마이그레이션·설정) 확인한다.
- ADR 0008 의 방식(develop 에서 `release/*` 를 따서 통째로 병합, 빼는 변경은 revert)은 사용자가 develop 전체 릴리스를 명시했을 때만 쓴다. revert 가 main → develop 되돌려 합칠 때 develop 에도 되돌려지기 때문이다.

### 2. release 브랜치 만들기 (선택 릴리스)
```bash
git switch -c release/<slug>-<UTC yyyymmddHHMM> origin/main
git cherry-pick -x <squash 커밋 SHA>          # 의존 순서대로. merge 커밋을 -m 으로 고르지 않는다
```
- 이미 main 에 같은 patch 가 있으면 제외하고 근거를 남긴다. 남는 것이 없으면 "이미 릴리스됨"으로 끝낸다.
- 생성 파일(`docs/openapi.json`, `.claude/rules/db-schema.sql`, `uv.lock`)이 충돌하면 손으로 합치지 말고 다시 생성한다. Alembic `heads` 가 1개인지 확인한다.
- `git diff origin/main...HEAD` 의 파일 목록이 선택한 작업의 범위와 같은지 확인한다. 이 diff 가 릴리스 범위다.

### 3. 선택한 트리에서 검증
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app scripts && uv run pytest
# 전용 DB(softbank_iris_test, alembic upgrade head 완료)로 통합 테스트·마이그레이션 왕복
TEST_DATABASE_URL=... uv run pytest -m integration
DATABASE_URL=... uv run alembic upgrade head && uv run alembic check
DATABASE_URL=... uv run alembic downgrade base && uv run alembic upgrade head
```
- develop 에서 통과한 것만으로는 부족하다. main + 선택한 커밋 조합에서 돌린다. DB 자격 증명은 출력하지 않는다.

### 4. PR (release → main)
- 브랜치를 push 하고 `gh pr create --base main` 으로 만든다. 본문에는 선택한 커밋(원본 SHA·PR), main 기준 SHA, 마이그레이션·의존성 영향, 실제로 돌린 검사, 배포 계획(컴포넌트)을 적는다. 관련 없는 develop 변경을 넣지 않는다.
- `check`·`database` 가 통과해야 한다. 병합 직전에 main SHA 가 기준과 같은지 다시 본다. 움직였으면 새 main 위에 다시 cherry-pick 해 검증을 반복한다(shared 브랜치 force push 금지).

### 5. 병합 (명시 승인 후)
```bash
gh pr merge <번호> --merge     # merge commit
git fetch origin && git rev-parse origin/main   # 실제 배포할 main SHA 기록
```

### 6. 배포
```bash
gh workflow run deploy-platform.yml --ref main -f api=true [-f build_worker=true] [-f deploy_worker=true]
gh run watch <run id> --exit-status
```
- `api` 는 DB 마이그레이션을 포함한다. Worker 코드(`app/workers/`, `build_service.py`, `deploy_service.py`, 그들이 쓰는 모델·클라이언트)가 바뀌었을 때만 해당 Worker 를 같이 고른다.
- 마이그레이션이 있으면 PreSync Job 이 실패했을 때 Argo sync 가 멈추고 이전 Pod 가 계속 돈다. 실패하면 원인을 보고하고, 사용자 확인 없이 DB 를 고치지 않는다.

### 7. 운영 검증 (증거)
- 워크플로 성공과 GitOps 저장소의 `deploy platform ...` 커밋(`Iris-Source-Sha` 가 배포한 main SHA 와 같은지).
- `curl -s -o /dev/null -w '%{http_code}' https://api.likelion.uk/healthz` 와 `/readyz` 가 204.
- 이번 변경이 실제로 동작하는지 확인한다(예: 바뀐 API 호출, 스키마 변경이면 그 컬럼을 쓰는 요청). 로그인이 필요한 API 는 Aside 로 `https://likelion.uk` 탭을 열고 `fetch(..., {credentials: 'include'})` 로 호출한다.

### 8. main 을 develop 에 되돌려 합치기
- `main` → `develop` PR(`chore: main 을 develop 에 되돌려 합침`)을 merge commit 으로 병합한다(CI 통과 후, develop PR 병합은 사전 승인 사항). 선택 릴리스는 cherry-pick 복제 커밋이 있지만 patch 가 같아 충돌하지 않는다.

## 완료 보고
릴리스 PR·선택한 커밋·기준/배포 SHA·실행한 검사·배포 run·운영 검증 결과를 구분해 적는다. 준비됨·병합됨·배포됨·검증됨을 섞어 쓰지 않는다. 확인하지 못한 항목은 못 했다고 적는다.

## 아직 확인하지 못한 것
- 운영 RDS 의 `alembic_version` 을 직접 읽는 경로. iris AWS 프로필 조회는 사용자가 거절한 적이 있어 묻지 않고는 하지 않는다. 마이그레이션 적용은 변경된 API 의 동작으로 간접 확인한다.
