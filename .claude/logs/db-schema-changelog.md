# DB 스키마 변경 로그 (db-schema-changelog)

`app/models/**/*.py` 변경 시 PostToolUse hook(`.claude/hooks/db_schema_changelog.py`)이 **한 줄**로 자동 기록한다. 상세 diff·스키마 DDL 은 git 이력과 [db-schema.sql](../rules/db-schema.sql) 을 본다.

형식: `- <UTC> · <파일> (<도구>) · <+추가 −삭제>`

<!-- 아래에 hook이 한 줄씩 prepend 한다 (이 마커 라인은 삭제하지 않는다) -->
<!-- CHANGELOG-ENTRIES -->
- 2026-10-03T14:34Z · app/models/deployment_request.py (Edit) · requested_deployment_strategy, deployment_strategy 컬럼 추가
- 2026-10-03T14:34Z · app/models/service.py (Edit) · deployment_strategy 컬럼 추가
- 2026-10-03T03:10Z · app/models/deployment_request.py (Edit) · service_upload_id 컬럼 추가
- 2026-10-03T03:10Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-03T03:10Z · app/models/__init__.py (Edit) · +1 −0
- 2026-10-03T03:10Z · app/models/service_upload.py (Write) · 신규/동일
- 2026-10-03T02:21Z · app/models/build.py (Edit) · log_tail 컬럼 추가
- 2026-10-03T02:21Z · app/models/build.py (Edit) · log_tail 컬럼 추가
- 2026-10-03T02:00Z · app/models/deployment_request.py (apply_patch) · scaling_snapshot JSONB 컬럼 추가
- 2026-10-03T02:00Z · app/models/service.py (apply_patch) · scaling_config JSONB 컬럼 추가
- 2026-10-03T02:09Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-03T02:09Z · app/models/__init__.py (Edit) · +5 −0
- 2026-10-03T01:22Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-03T01:22Z · app/models/__init__.py (Edit) · +1 −0
- 2026-10-03T01:21Z · app/models/deployment_diagnosis.py (Write) · deployment_diagnoses 테이블 신규(deployment_request_id, requested_by, status, result, error_code, finished_at)
- 2026-10-03T01:18Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-03T01:18Z · app/models/__init__.py (Edit) · +1 −0
- 2026-10-03T01:18Z · app/models/cli_login_session.py (Write) · cli_login_sessions 테이블 신규(public_id, poll_secret_hash, status, user_id, expires_at, consumed_at, last_polled_at)
- 2026-10-02T13:12Z · app/models/__init__.py (Edit) · +2 −0
- 2026-10-02T13:12Z · app/models/__init__.py (Edit) · +1 −0
- 2026-10-02T13:12Z · app/models/service_variable.py (Write) · 신규/동일
- 2026-10-02T10:30Z · app/models/release.py (Write) · build_id, revert_commit_sha, failure_code, deadline_at, finished_at 컬럼과 진행 중 release 유일 index 추가
- 2026-10-02T10:30Z · app/models/build.py (Write) · status, source_sha, attempt, image_tag, deploy_config, failure_code 컬럼 추가, builder nullable
- 2026-10-02T10:30Z · app/models/deployment_request.py (Edit) · cancel_requested_at 컬럼 추가
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

- 2026-10-03: `deployment_repairs` 추가. 배포·성공한 진단 FK, 서비스 범위 멱등 키, 배포별 RUNNING 부분 unique index, 고정 원문/소스/정책·digest, generation claim 및 UNKNOWN_OUTCOME 기록. revision `9f81c52a01bd`; DDL 동기화.
- 2026-10-03: `services.deployment_strategy`(NOT NULL, 기본 ROLLING)·`deployment_requests.requested_deployment_strategy`·`deployment_strategy`(nullable) 추가, 각각 CHECK 제약. revision `f49792bf1fcc`; DDL 동기화.
- 2026-10-04: `repository_analyses` 추가(레포 구성 분석. 프로젝트·사용자·설치 FK, 고정 source_sha, mode·status·decision·complexity·error_code CHECK, 분석기 응답 result·applied_service_ids JSONB, attempts·locked_by·locked_until lease, QUEUED·RUNNING 부분 index, NOTIFY jobs 트리거). revision `061a382166d5`; DDL 동기화.
