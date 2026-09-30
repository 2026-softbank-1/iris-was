# DB 마이그레이션 컨벤션 (PostgreSQL · Alembic)

- **적용 대상**: 스키마 변경(테이블·컬럼·인덱스·제약·Enum)이 있는 모든 작업
- **함께 보기**: 모델 작성은 [backend-conventions.md](backend-conventions.md) §2.5, 스키마 단일 출처는 [db-schema.sql](db-schema.sql)

> **핵심**: 스키마 변경은 **모델 수정 → Alembic revision → db-schema.sql 동기화** 세 가지가 한 세트다. DB에 직접 DDL을 실행하지 않는다.

---

## 1. 구성

| 항목 | 규칙 |
|---|---|
| DB | PostgreSQL. 드라이버는 `asyncpg` (`postgresql+asyncpg://...`) |
| 초기화 | `uv run alembic init -t async alembic` (async 템플릿) |
| 위치 | 레포 루트의 `alembic/`, `alembic.ini` |
| 접속 URL | `alembic/env.py` 에서 `get_settings().database_url` 로 주입. `alembic.ini` 에 URL·비밀번호를 적지 않는다 |
| 메타데이터 | `env.py` 의 `target_metadata = Base.metadata`. 모든 모델은 `app/models/__init__.py` 에서 import 해 autogenerate 가 인식하게 한다 |
| 파일명 | `alembic.ini` 의 `file_template = %%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(rev)s_%%(slug)s` — 시간순 정렬 |

### 제약 이름 규칙 (필수)

익명 제약은 autogenerate 가 감지하지 못한다. `app/models/base.py` 의 `MetaData` 에 naming convention 을 건다.

```python
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
```

---

## 2. 작업 절차

```bash
# 1) app/models/*.py 수정
# 2) revision 생성 (메시지는 영문 소문자, 무엇을 바꾸는지)
uv run alembic revision --autogenerate -m "add status to deployment"
# 3) 생성된 파일을 반드시 열어 검토·수정 (§3)
# 4) .claude/rules/db-schema.sql 동기화
# 5) 로컬 DB 에 적용 → 되돌리기 → 재적용으로 downgrade 검증
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

- 모델과 DB 차이가 남았는지 확인: `uv run alembic check` (DB 접속 필요, autogenerate 와 같은 비교 로직)
- 브랜치 병합 후 head 가 2개면: `uv run alembic heads` 로 확인 → `uv run alembic merge heads -m "merge heads"`

---

## 3. autogenerate 검토 포인트

autogenerate 결과를 그대로 커밋하지 않는다. 아래는 **감지하지 못하거나 잘못 만드는** 경우다.

| 변경 | autogenerate 결과 | 조치 |
|---|---|---|
| 테이블·컬럼 이름 변경 | drop + add (데이터 유실) | `op.rename_table` / `op.alter_column(new_column_name=)` 로 직접 수정 |
| 익명 제약 | 감지 못 함 | naming convention 적용(§1) |
| PostgreSQL ENUM 값 추가·변경 | 감지 못 함 | `op.execute("ALTER TYPE ... ADD VALUE ...")` 직접 작성 |
| CHECK 제약 | 기본 감지 안 함 | 직접 작성 |

---

## 4. 작성 규칙

- **1 revision = 1 논리적 변경.** 관련 없는 변경을 한 파일에 섞지 않는다.
- **`downgrade()` 를 반드시 구현**한다. 되돌릴 수 없으면(데이터 삭제 등) 이유를 docstring 에 적고 `raise NotImplementedError`.
- **main 에 머지됐거나 어느 환경에든 적용된 revision 은 수정하지 않는다.** 고칠 게 있으면 새 revision 을 만든다.
- 마이그레이션 파일에서 `app.models` 를 import 하지 않는다. 모델이 바뀌면 과거 revision 이 깨진다. 필요하면 `sa.table()` 로 그 시점 구조를 직접 선언한다.
- **스키마 변경과 데이터 변경(backfill)은 revision 을 분리**한다.
- NOT NULL 컬럼 추가는 `server_default` 를 주거나, nullable 추가 → backfill → NOT NULL 순서로 나눈다.
- 시각 컬럼은 `DateTime(timezone=True)` (`timestamptz`).
- 운영 중 큰 테이블 인덱스는 `CONCURRENTLY` 로 만든다. 트랜잭션 밖에서 실행해야 하므로 `with op.get_context().autocommit_block():` 안에서 `op.create_index(..., postgresql_concurrently=True)`.

---

## 5. 체크리스트

1. 모델·revision·db-schema.sql 이 같은 PR 에 있는가?
2. autogenerate 결과를 §3 기준으로 검토했는가?
3. `downgrade()` 가 있고 로컬에서 upgrade → downgrade → upgrade 가 통과하는가?
4. head 가 1개인가?
