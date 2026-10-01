# DB 스키마 변경 로그 (db-schema-changelog)

`app/models/**/*.py` 변경 시 PostToolUse hook(`.claude/hooks/db_schema_changelog.py`)이 **한 줄**로 자동 기록한다. 상세 diff·스키마 DDL 은 git 이력과 [db-schema.sql](../rules/db-schema.sql) 을 본다.

형식: `- <UTC> · <파일> (<도구>) · <+추가 −삭제>`

<!-- 아래에 hook이 한 줄씩 prepend 한다 (이 마커 라인은 삭제하지 않는다) -->
<!-- CHANGELOG-ENTRIES -->
- 2026-10-01T11:23Z · app/models/{user,service,deployment_request,job}.py (Bash) · users·services·deployment_requests·jobs 테이블 신규 (revision cf3b3859224c)
- 2026-10-01T11:25Z · app/models/__init__.py (Write) · +6 −1
- 2026-10-01T11:25Z · app/models/build.py (Write) · 신규/동일
- 2026-10-01T11:24Z · app/models/base.py (Write) · created_at, updated_at 컬럼 추가
