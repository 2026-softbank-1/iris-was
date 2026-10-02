# DB 스키마 변경 로그 (db-schema-changelog)

`app/models/**/*.py` 변경 시 PostToolUse hook(`.claude/hooks/db_schema_changelog.py`)이 **한 줄**로 자동 기록한다. 상세 diff·스키마 DDL 은 git 이력과 [db-schema.sql](../rules/db-schema.sql) 을 본다.

형식: `- <UTC> · <파일> (<도구>) · <+추가 −삭제>`

<!-- 아래에 hook이 한 줄씩 prepend 한다 (이 마커 라인은 삭제하지 않는다) -->
<!-- CHANGELOG-ENTRIES -->
- 2026-10-02T01:41Z · app/models/service_analysis.py (apply_patch) · service_analyses 분석 작업·결과·lease 테이블 추가 (revision 95064db3600f)
- 2026-10-02T01:41Z · app/models/__init__.py (apply_patch) · ServiceAnalysis metadata 등록
- 2026-10-01T15:23Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-01T15:23Z · app/models/__init__.py (Edit) · +1 −0
- 2026-10-01T15:22Z · app/models/deployment_request.py (Edit) · +6 −0
- 2026-10-01T15:22Z · app/models/deployment_status_history.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/release.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/build.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/job.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/deployment_request.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/service.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/target.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/project.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/user.py (Write) · 신규/동일
- 2026-10-01T02:33Z · app/models/base.py (Write) · created_at, updated_at, is_deleted, deleted_at 컬럼 추가
