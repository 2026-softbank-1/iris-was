# 0019. Worker 는 5초 polling 대신 jobs 트리거의 LISTEN/NOTIFY 로 깨운다

- 상태: 제안됨
- 날짜: 2026-10-03
- 결정자: 김현겸

## 배경
Build·Deploy Worker 는 일이 없어도 5초마다 선점 쿼리(전역 advisory lock 포함)를 실행했다. 요청 → BUILD → DEPLOY → RECONCILE 로 넘어갈 때마다 최대 5초씩 늦고, idle 에도 Worker 당 분당 12번 DB 를 두드린다.

## 검토한 선택지
1. **SQS.** 큐·IAM·Pod Identity·CI 권한·chart 를 iris-infra 에 추가하고, Control API 에 처음으로 AWS 권한(`sqs:SendMessage`)을 줘야 한다. 사용자별 동시 빌드 제한·lease·"빌드 성공 → DEPLOY job 생성" 트랜잭션 때문에 `jobs` 는 그대로 진실 원본이어야 해서, SQS 는 깨우기 신호 역할만 한다. 그런데도 "commit 뒤 발행"을 7곳 넘는 지점에서 지키고 유실 대비 sweeper 를 둬야 한다.
2. **앱 코드에서 `pg_notify` 호출.** 인프라 변경은 없지만 job 생성·상태 변경 지점마다 넣어야 하고, 하나를 빠뜨리면 그 경로만 늦어진다.
3. **jobs 트리거 + LISTEN.** 트리거 하나가 모든 경로를 덮고, 알림이 commit 시점에만 나가는 것을 DB 가 보장한다.

## 결정
3번. 설계 원문 §5 의 "`LISTEN/NOTIFY` 는 즉시 깨우기 용도로만" 과 같다. 선점 쿼리(`claim_next_job`)와 `jobs` 의 의미는 바꾸지 않는다.

- 트리거(`notify_job_change`)는 INSERT, `RUNNING → QUEUED·RETRY_WAIT`(반납·snooze·재시도), BUILD 종료에서 `NOTIFY jobs, <kind>` 를 보낸다. 재시도·snooze 를 알려야 자고 있던 Worker 가 새 `run_after` 로 대기 시간을 다시 계산한다. 끝난 BUILD 는 사용자별 빌드 제한에 막혀 있던 BUILD 를 풀어 준다. 선점(`QUEUED → RUNNING`)·lease 갱신·BUILD 외 job 의 종료는 다른 Worker 를 헛되이 깨우므로 알리지 않는다.
- Worker 는 SQLAlchemy 풀과 별개인 asyncpg 연결 하나로 LISTEN 하고, 자기 kind 알림만 받는다(`app/workers/job_wakeup.py`).
- 선점할 job 이 없으면 알림·가장 이른 미래 `run_after`(재시도 backoff·RECONCILE 10초 snooze)·60초 fallback 중 먼저 오는 때까지 기다린다. 이미 지난 `run_after` 는 보지 않는다. 남아 있다면 빌드 제한에 막힌 것이라, 포함하면 바쁜 루프가 된다.
- 선점 **전에** 알림 표시를 지운다. 선점 중 도착한 알림을 놓치지 않는다.
- 남은 시간은 DB 시계(`run_after - now()`)로 계산해 Pod 와의 시계 차이로 일찍 깨어 헛도는 일을 막는다.
- 리스너는 대기 때마다 `SELECT 1` 로 확인한다(RST 없이 끊긴 연결 감지). 새로 LISTEN 을 시작한 직후에는 그 전 알림을 놓쳤을 수 있어 바로 다시 선점한다. 리스너를 열 수 없거나 선점·조회가 DB 오류로 실패하면 알림을 믿지 않고 예전처럼 5초 간격으로 시도한다.

## 결과
- 단계 전환 지연이 알림 전달 시간 수준이 되고, idle 의 선점 쿼리는 Worker 당 분당 약 1번으로 준다.
- iris-infra·Control API 변경이 없다.
- 알림은 저장되지 않는다. 리스너가 끊긴 동안의 알림과 Pod 강제 종료 후 만료된 lease 는 60초 fallback 으로 회수한다. lease 회수는 최대 5분 5초에서 6분으로 늘어난다.
- Worker 프로세스마다 DB 연결이 1개 늘어난다. PgBouncer(transaction pooling)를 도입하면 리스너는 RDS 에 직접 붙여야 한다.
- 롤백할 때 이전 이미지에는 이 revision 파일이 없다. digest 만 되돌리면 PreSync migration 이 실패하므로 먼저 새 이미지로 `alembic downgrade -1` 을 실행한다. 트리거만 남은 상태는 무해하므로, 급하면 Worker digest 만 되돌린다.
- SQS 는 다른 시스템이 job 을 넣거나 받아야 할 때, DB 와 분리된 버퍼·큐 지표가 필요할 때 다시 검토한다.
