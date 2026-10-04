# 0031. 관리형 DB 는 레포의 `/docker-entrypoint-initdb.d` 스크립트를 첫 기동에 한 번 실행한다

- 상태: 제안됨 (iris-infra chart 0.8.0 `database.initScripts` 반영과 함께 ADR 0030 롤아웃에 포함)
- 날짜: 2026-10-04
- 결정자: 김우현
- 근거: 통합 설계 `DESIGN-phase2-databases-env.md` 계약 F, ADR 0030 D-4

## 배경
ADR 0030 으로 플랫폼이 만든 postgres 는 비어 있다. 통합 검증에서 쇼핑몰 worker 가 `relation "jobs" does not exist` 로 실패했다. compose 는 `./db/schema.sql:/docker-entrypoint-initdb.d/001-schema.sql` 처럼 스키마·seed 를 넣고, 공식 postgres·mysql·mongo 이미지는 **데이터 디렉터리가 비어 있을 때 한 번만** 그 디렉터리의 스크립트를 이름순으로 실행한다.

## 검토한 선택지
1. 플랫폼이 배포 뒤 Job 으로 스크립트를 실행한다 — 실행 여부·멱등성을 플랫폼이 추적해야 하고 DB 접속 경로가 하나 더 생긴다.
2. 이미지의 initdb 동작을 그대로 쓴다. chart 가 ConfigMap `app-initdb` 를 `/docker-entrypoint-initdb.d` 에 mount 한다 — 한 번만 실행되는 것은 이미지가 보장한다.
3. 내용 저장: 분석·서비스마다 행을 복사한다 vs sha256 으로 한 번만 저장하고 메타데이터가 가리킨다.

## 결정
2 와 sha256 저장(3 의 뒤쪽)을 택한다.

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | 분석기는 `dependencies[].initScripts[{path, kind, sha256, size, order, supported}]` 만 내고 내용은 내지 않는다. Build Worker 는 소스를 푼 상태에서 supported 행의 파일을 읽어 경로(레포·분석 위치 안, 링크 없음)·크기·sha256·종류(postgres·mysql `.sql`·`.sql.gz`, mongodb `.js`)·한도(파일·DB 합계 1 MiB, DB 당 20개, 분석당 8 MiB)를 다시 확인한다. 실패한 행은 분석 결과에서 `supported: false` 로 바꾼다. DB 합계가 넘으면 그 DB 의 스크립트를 모두 뺀다 | 분석기 출력은 신뢰 경계 밖이다. 일부만 넣으면 스키마 없이 seed 만 도는 식으로 어긋난다 |
| D-2 | 내용은 `database_init_scripts(sha256 PK, size_bytes, content bytea)` 에 같은 내용 한 번만 둔다(분석 성공을 기록하는 트랜잭션). 분석 결과와 DB 서비스 `database_config.initScripts[{name, path, kind, sha256, size}]` 는 sha256 으로 가리킨다 | push 마다 재분석해도 내용이 같으면 늘지 않는다. 바이너리(`.sql.gz`)를 그대로 둔다 |
| D-3 | apply 가 **새로 만드는** DB 에만 메타데이터를 복사한다. 이름은 `{order:02d}-{basename}`(chart 규칙 `^[0-9]{2}-[A-Za-z0-9._-]+\.(sql\|sql\.gz\|js)$`, 확장자는 kind). `POST /databases` 로 만든 DB 는 스크립트가 없다 | 순서는 분석기 order(컨테이너 파일명 순)를 그대로 쓴다 |
| D-4 | 이미 있는 DB 는 스크립트가 달라져도 고치지도 다시 실행하지도 않는다. push 재분석은 스택 `pendingChanges` 에, 증분 apply 는 응답 `changes` 에 `DEPENDENCY_CHANGED`(field `initScripts`, reason `init_scripts_changed`, 안내 `message`, `from`/`to` = 실행될 `[{path, sha256}]`)로 알린다 | 이미 초기화된 데이터 디렉터리에서는 이미지가 어차피 실행하지 않는다. 바꾸려면 DB 를 지우고(데이터 소실) 다시 apply 한다 |
| D-5 | Deploy Worker 는 DB values `database.initScripts[{name, content}]`(UTF-8 텍스트) 또는 `{name, binaryContent}`(base64, `.sql.gz`·UTF-8 아닌 파일)를 쓴다. 원본 바이트 합계가 ConfigMap 한도(1 MiB)를 넘거나 내용이 없거나 해시가 다르면 재시도 없이 실패한다(커밋 전) | chart 는 합계를 검사하지 않는다. 다시 해도 같은 결과다 |

## 결과
- 마이그레이션 `663b24ad4296`(`database_init_scripts`). 롤아웃은 ADR 0030 순서와 같다(chart 0.8.0 이 `database.initScripts` 를 받은 뒤 `PROJECT_NETWORKING_ENABLED`).
- 스크립트 내용은 API 응답·로그에 나가지 않는다. 서비스 응답 `database.initScripts` 는 이름·경로·sha256·크기만이다.
- ConfigMap 이 바뀌어 Pod 가 다시 떠도 데이터 디렉터리가 있으면 실행되지 않는다(chart 문서).
- ponytail: `database_init_scripts` 는 참조가 사라진 행을 지우지 않는다(같은 내용 중복이 없어 증가는 서로 다른 스크립트 수만큼이다).
- ponytail: `.sh` 스크립트는 실행하지 않는다(분석기가 `init_script_unsupported` 질문을 낸다).
