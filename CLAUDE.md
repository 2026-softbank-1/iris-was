# WAS — Team Iris 배포 자동화 플랫폼 API 서버

- **스택**: Python 3.13+, FastAPI, Pydantic v2, SQLAlchemy 2.0(async), PostgreSQL(asyncpg), Alembic
- **배경·환경변수·실행·세팅**: [README.md](README.md) 참조 (여기서 중복하지 않는다)

---

## 규칙 문서 (반드시 따른다)

작업 전 아래 문서를 우선한다. **충돌 시 우선순위: 컨벤션 가이드 > 일반 관례.**

| 문서 | 언제 본다 |
|---|---|
| [.claude/rules/db-schema.sql](.claude/rules/db-schema.sql) | 테이블·컬럼·타입·제약·인덱스 등 **물리 DB 스키마(DDL)의 단일 출처**. `app/models/*.py` 모델은 이 스키마와 일치해야 하며, 모델 변경은 자동으로 [.claude/logs/db-schema-changelog.md](.claude/logs/db-schema-changelog.md) 에 기록된다 |
| [.claude/rules/db-migration.md](.claude/rules/db-migration.md) | 스키마를 바꿀 때 — Alembic revision 생성·검토·downgrade 규칙, PostgreSQL 주의점 |
| [.claude/rules/backend-conventions.md](.claude/rules/backend-conventions.md) | 백엔드 코드(레이어·네이밍·비동기·예외·로깅·설정·테스트·린터)를 작성할 때 |
| [.claude/rules/git-conventions.md](.claude/rules/git-conventions.md) | 커밋 메시지·브랜치명을 정할 때 — Conventional Commits 규칙 |

---

## 핵심 원칙 (요약)

코드를 쓰기 전 위 문서를 보되, 자주 어기는 핵심만 추린다.

### 네이밍·식별자
- 필드·변수·컬럼은 **`snake_case`**. camelCase 금지(프론트 경계에서만 alias 변환).
- 레이어는 접미사로 드러낸다: `_router` / `Service` / `Client` / `Repository` / Model·Schema.
- 약어는 클래스명에서 첫 글자만 대문자(`HttpClient`, `AwsClient`), 필드는 전부 소문자.
- 동사로 반환 형태를 고정: `get_*`(1건/없으면 예외) · `find_*`(nullable) · `search_*`(다건) · `check_*`/`validate_*`.

### 레이어 경계
- 흐름: `Router → Service → Repository → DB` / Service → Client(외부 API).
- Router는 얇게(로직 없음, Service 호출 + Schema 변환만). Repository는 Model만 반환. Model은 Schema를 모른다.
- 외부 호출은 반드시 **비동기 Client**를 통한다. Service가 `httpx`·SDK를 직접 부르지 않는다.
- 변환은 한 방향만, 대상 타입의 classmethod로(`Response.from_model`).
- 모든 JSON 응답은 공통 봉투 **`ApiResponse[T]`**로 감싼다(`response_model_exclude_none=True`). 파일 다운로드·204는 제외. 에러는 예외 핸들러가 `error_body`(도메인 예외의 `code` 포함)로 통일한다.

### 공통 안전 규칙
- I/O는 전부 `async def` + `await`. 동기 블로킹 호출은 `asyncio.to_thread`로 감싼다.
- 시각은 항상 **timezone-aware UTC**(`datetime.now(UTC)`). `utcnow()` 금지.
- 스키마 변경은 **모델 → Alembic revision → db-schema.sql** 한 세트. DB에 직접 DDL 금지, 적용된 revision 수정 금지.
- 시크릿은 `BaseSettings`로 주입. `os.getenv` 직접 호출·하드코딩 금지. 로그·예외에 시크릿 노출 금지.
- 예외는 `AppError` 계층으로 raise, HTTP 매핑은 핸들러 한곳에서. 빈 `except`·예외 뭉개기 금지.
- 로깅은 구조화형(`extra`로 `service`·`action` 필드 분리). `print` 금지.
- 모든 함수 시그니처에 타입 힌트. 신문법(`str | None`, `list[T]`) 사용.
- 주석·docstring에 규칙 문서 위치(`backend-conventions §3` 등)를 인용하지 않는다.

---

## 도구

- **패키지·실행**: `uv`. 명령은 `uv run <cmd>` — 린트 `uv run ruff check .` / 타입 `uv run mypy app` / 테스트 `uv run pytest` / 서버 `uv run uvicorn app.main:app` / 마이그레이션 `uv run alembic upgrade head`.
- **린트·포맷**: `ruff`. **타입체크**: `mypy`. 설정은 `pyproject.toml` 한곳.
- **테스트**: `pytest` + `pytest-asyncio`. 함수명 `test_{대상}_{시나리오}_{기대}`.
- **품질 게이트**: Claude Code PreToolUse 훅(`.claude/settings.json`)이 `git add`·`git commit` 전 `ruff`·`mypy`·`pytest`, `git push` 전 `ruff`를 돌려 실패 시 차단한다. `pyproject.toml`이 없으면 건너뛴다.
- **스키마 동기화**: `app/models/*.py` 변경 시 PostToolUse 훅(`.claude/hooks/db_schema_changelog.py`)이 변경을 `.claude/logs/db-schema-changelog.md`에 기록하고 Alembic revision 생성과 `.claude/rules/db-schema.sql` 동기화를 지시한다.
- **PR 본문**: `write-pr` 스킬(`.claude/skills/write-pr/`)이 `.github/PULL_REQUEST_TEMPLATE.md` 구조로 `docs/PR.md`를 작성한다.
