# Git 컨벤션 (커밋 메시지 · 브랜치명)

- **적용 대상**: 이 레포의 모든 커밋과 브랜치
- **함께 보기**: PR 본문은 [.github/PULL_REQUEST_TEMPLATE.md](../../.github/PULL_REQUEST_TEMPLATE.md) 를 따른다. 이 문서는 **커밋·브랜치**만 다룬다.

> **핵심**: 커밋 메시지는 [Conventional Commits](https://www.conventionalcommits.org/) 를 따른다.

---

## 1. 커밋 메시지

### 형식

```text
<type>: <설명>
```

- `<type>` 은 아래 표의 Conventional Commits 타입.
- `<설명>` 은 한글/영문 모두 허용, 명령형으로 간결하게. 마침표로 끝내지 않는다.

### 예시

```text
feat: 배포 파이프라인 API 구현
fix: 배포 상태 조회 오류 수정
chore: ruff 설정 추가
```

### type 목록

| type       | 용도                                  |
| ---------- | ----------------------------------- |
| `feat`     | 기능 추가                              |
| `fix`      | 버그 수정                             |
| `docs`     | 문서만 변경                           |
| `style`    | 포맷·세미콜론 등 동작 변화 없는 변경     |
| `refactor` | 기능 변화 없는 구조 개선               |
| `test`     | 테스트 추가·수정                       |
| `chore`    | 빌드·설정·의존성 등 부수 작업           |
| `perf`     | 성능 개선                            |

> 본문(body)·꼬리말(footer)이 필요하면 제목 다음 빈 줄 뒤에 작성한다(선택).

---

## 2. 브랜치명

### 형식

```text
<type>/<설명>
```

- `<type>` 은 커밋 type 목록과 같다.
- `<설명>` 은 **소문자 + 하이픈(`-`)** 으로 연결한다. 대문자·공백·언더스코어 금지.

### 예시

```text
feat/deploy-pipeline
fix/release-status
```

---

## 3. 브랜치 전략 (develop → release → main)

배경·대안: [docs/adr/0008](../../docs/adr/0008-branch-strategy.md)

```text
main ────────────────────────────●──────  (릴리스만. 항상 배포 가능)
  \                              ↑ merge        ↓ back-merge
   develop ──●──●──●──●──────────┴─ release/* ─ (통합 테스트·수정)
              ↑  ↑  ↑
           feat/* fix/* … (에이전트·사람별 워크트리, PR 로 squash merge)
```

| 브랜치 | 시작점 | 병합 대상 | 병합 방식 |
|---|---|---|---|
| `feat/*` `fix/*` 등 작업 브랜치 | `develop` | `develop` | PR, **squash merge** (작업 1개 = 커밋 1개) |
| `release/<설명>` (예: `release/prelim-1`) | `develop` | `main` | PR, **merge commit** |
| `main` | — | — | 직접 push 금지. 병합 후 `main` 을 `develop` 에 되돌려 합친다 |

### 에이전트·병렬 작업
- 작업(에이전트)마다 워크트리를 따로 만든다: `git worktree add ../iris-was-wt/<브랜치명> -b <브랜치명> develop`
- 작업 시작 전과 PR 전에 `develop` 을 받아 rebase 한다. 오래 두지 않는다.
- PR 대상은 항상 `develop`, 제목은 Conventional Commits 형식(squash 후 그대로 커밋 메시지가 된다).
- 끝난 워크트리는 `git worktree remove` 로 정리한다.

### 충돌이 잦은 파일
- **Alembic**: revision 은 한 번에 한 사람(에이전트)만 만든다. 머지 전 `uv run alembic heads` 가 1개인지 확인하고, 2개면 rebase 해서 `down_revision` 을 잇는다.
- **생성 파일**(`docs/openapi.json`, `.claude/rules/db-schema.sql`, `uv.lock`): 충돌 나면 손으로 합치지 않고 다시 생성한다(`uv run python -m scripts.export_openapi` · `alembic upgrade --sql` · `uv lock`).
- 공용 조립 파일(`app/dependencies.py`, `app/main.py`)은 변경을 작게 유지한다.

### 릴리스
1. `develop` 에서 `release/<설명>` 을 딴다.
2. release 에서 CI 전체와 통합 테스트, 마이그레이션 왕복(`upgrade head` → `alembic check` → `downgrade base` → `upgrade head`)을 확인하고, 수정은 release 에 직접 커밋한다.
3. `release/*` → `main` PR 을 merge commit 으로 병합한다. 릴리스에서 빼야 할 변경이 있으면 cherry-pick 이 아니라 그 변경을 revert 한다.
4. `main` 을 `develop` 에 되돌려 합친다(release 에서 한 수정 반영).

---

## 4. 체크리스트

1. 커밋 type 이 표에 있는가? 설명이 명령형·간결한가?
2. 브랜치가 `<type>/<소문자-하이픈-설명>` 형식이고 `develop` 에서 땄는가?
3. PR 대상이 `develop`(릴리스는 `main`)인가? 머지 전 `alembic heads` 가 1개인가?
