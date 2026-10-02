# 0011. 코드 분석은 별도 작업으로 저장하고 서비스 설정은 명시적으로 확정한다

- 상태: 제안됨
- 날짜: 2026-10-02
- 결정자: 통합 구현 담당

## 배경

코드 분석기는 고정된 소스의 서비스 후보, 명령·포트·환경변수, 근거 검증과 배포 초안을 반환한다. 기존 `jobs`는 배포 요청에 종속되고 BUILD·DEPLOY·RECONCILE·ROLLBACK을 처리하므로 서비스 등록 단계의 분석 대기·오류를 그대로 담기 어렵다. 모델 실행 성공과 분석의 `complete/needs_input/unsupported`, 배포 승인은 서로 다른 판단이다.

## 검토한 선택지

1. 분석 결과만 `services.analysis_plan`에 덮어쓴다 — 구현은 작지만 접수 소스, 실패·재시도, 이전 결과와 확인 기록을 잃는다.
2. 배포 `jobs`에 ANALYZE를 추가한다 — 기존 큐를 쓰지만 아직 배포 요청이 없는 서비스 분석까지 배포 상태에 연결된다.
3. `service_analyses`에 분석 작업과 결과를 보관한다 — 별도 큐가 필요하지만 분석·배포 상태와 서비스 설정 확정을 분리한다.

## 결정

3번을 사용한다. UUID 작업 ID, 서비스·요청자, 접수 당시의 저장소 URL·브랜치·고정 SHA·서비스 루트·외부 GitHub installation ID를 저장한다. 원본 소스나 자격증명은 이 테이블에 저장하지 않는다.

- 작업 상태는 `AnalysisJobStatus`의 QUEUED·RUNNING·SUCCEEDED·FAILED·CANCELLED다. 분석 내용 상태는 별도 `analysis_status`다. 정보 부족·미지원 결과도 실행이 정상 종료됐으면 SUCCEEDED이며 배포 상태를 바꾸지 않는다.
- 서비스당 QUEUED/RUNNING 작업은 부분 유니크 인덱스로 하나만 허용한다. 접수·확인 권한은 서비스 소유 관계를 확인하는 Service 계층에서 검사한다.
- Worker는 `FOR UPDATE SKIP LOCKED`로 대기 또는 lease가 만료된 작업을 선점한다. 선점마다 새 `lease_token`, 만료 시각, attempts를 기록한다. 갱신·진행·결과 저장은 RUNNING·현재 token·미만료 lease를 모두 확인해야 하며, 오래된 Worker의 결과는 채택하지 않는다. Repository는 commit하지 않고 호출자의 트랜잭션에 참여한다.
- 분석 결과, 검증 보고서, readiness, 배포 dossier, 실행 기록, 마스킹 근거를 각각 보관한다. snapshot·context·result digest를 통해 다른 입력의 결과를 섞지 않는다. readiness의 context hash는 추가 근거 확보로 달라질 수 있지만 같은 source snapshot이어야 한다.
- AI 작업의 provider·model·outputMode·timeoutSeconds 등 허용된 비밀이 아닌 설정은 접수 시 `model_selection`에 고정한다. Worker는 실행 설정이 같은 선택인지 확인한 뒤 호출하며, 다른 설정으로 조용히 전환하지 않는다. static 작업은 이 필드가 null이고 자격증명은 어떤 작업에도 저장하지 않는다.
- 빌더·명령·포트와 저장소 안의 candidate는 추천이다. 사용자가 선택한 candidate와 `confirmed_at`을 기록하고 검증된 명시 확인 경로에서만 서비스 설정에 반영한다. 분석 완료가 빌드·배포 승인을 부여하지 않는다.
- 원본 소스 수집과 모델 실행은 Client 경계에서 처리한다. 모델 실패를 정적 분석 성공으로 대체하지 않고 실제 실행 모드·오류를 기록한다. 실제 빌드·클라우드 변경·자동 수정은 기존 서비스의 후속 흐름이다.

## 결과

분석 실행과 사용자 확인을 배포 요청 없이 조회할 수 있고, 실패한 분석도 당시 소스와 함께 남는다. 모델 호출 중 DB 잠금을 유지하지 않으며 lease 갱신은 별도 짧은 트랜잭션으로 처리한다. 만료 후 다시 선점된 작업은 재실행될 수 있으므로 공유 모델 예산과 한도를 계속 적용한다. 배포 큐와 분석 큐의 운영·청소 정책은 각각 필요하다. 이 변경은 로그 오류 진단 작업이나 자동 수정 루프를 구현하지 않는다.
