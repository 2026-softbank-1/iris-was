# 0008. 브랜치 전략: develop 통합, release 에서 검증 후 main 병합

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
여러 에이전트가 동시에 기능을 구현한다. 작업이 서로 간섭하지 않고, main 은 검증된 상태만 갖도록 하는 브랜치 전략이 필요하다. 현재 저장소는 `main` 하나뿐이다. 예선 1차(10/3~10/4)까지 일정이 짧다.

## 검토한 선택지
1. **main 에서 바로 기능 브랜치 → PR → main.** 가장 단순하지만 통합 검증 단계가 없다.
2. **develop 통합 + release 에서 구현 커밋만 cherry-pick 해 취합 → main.** 원래 제안안. 다음 문제가 있었다.
   - Alembic revision 이 병렬로 만들어져 head 가 갈라지고, cherry-pick 순서가 바뀌면 `down_revision` 체인이 깨진다.
   - `db-schema.sql`·`openapi.json`·`uv.lock`·`dependencies.py` 등 공용 파일에서 커밋마다 충돌한다.
   - 커밋 간 의존성 때문에 일부만 취합하면 develop 에서 통과한 조합과 달라진다.
   - cherry-pick 은 복제 커밋을 만들어 이후 병합에서 같은 충돌이 반복된다.
3. **develop 통합 + develop 에서 release 분기 + main 병합 (선택).** 선택지 2 의 구조를 유지하되 취합을 cherry-pick 대신 브랜치 분기로 한다.

## 결정
선택지 3.
- 작업(에이전트)마다 `develop` 에서 워크트리·브랜치를 만들고, PR 을 **squash merge** 로 `develop` 에 넣는다.
- 릴리스는 `develop` 에서 `release/<설명>` 을 따서 통합 테스트·마이그레이션 왕복을 확인하고 `main` 에 merge commit 으로 병합한다. 빼야 할 변경은 cherry-pick 이 아니라 revert 한다.
- 병합 후 `main` 을 `develop` 에 되돌려 합친다.
- 마이그레이션은 한 번에 한 작업만 만들고, 생성 파일 충돌은 재생성으로 푼다.
- GitHub Actions CI(ruff·mypy·pytest·마이그레이션 검증)를 PR 필수 검사로 둔다.
- 세부 절차는 `.claude/rules/git-conventions.md` 를 따른다.

## 결과
- 병렬 작업이 격리되고 main 은 release 검증을 거친 상태만 가진다.
- squash 로 작업 1개가 커밋 1개가 되어 revert 와 추적이 쉽다.
- 팀과의 별도 협의 없이 이 방식으로 진행하기로 했다. 이견이 나오면 새 ADR 로 바꾼다.
- **후속 작업(GitHub 설정):** `develop` 브랜치 푸시, `main`·`develop` 브랜치 보호(직접 push 금지, CI 필수, PR 필수), 기본 브랜치를 `develop` 으로 변경할지 결정. 저장소 관리자 권한이 필요하다.
