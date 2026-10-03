# 0026: AI 수정 클릭은 핫픽스 생성과 main 머지까지 승인한다

상태: 제안됨

처음 GitHub App 연결에서 Contents와 Pull requests read/write를 요청한다. 웹은 로그인·저장소
연결 단계에서 두 권한과 자동 수정/머지를 안내한다. 설치에서 승인한 사용자는 AI 수정 버튼을
누르면 후보 생성부터 핫픽스 브랜치, PR, main 머지까지 진행하며 별도 후보·PR·머지 확인을 받지 않는다.

`POST /services/{serviceId}/deployments/{deploymentId}/auto-repair`는 diagnosisId만 받는다.
서버가 원본 진단에서 코드 변경만으로 이루어진 계획을 고르고, 소유권·쓰기 권한·main의 고정
실패 SHA를 확인한 뒤 generation 요청과 `autoMerge=true`, 30분 마감시각을 한 트랜잭션으로 저장한다.
권한 부족이나 stale main은 모델 호출 전에 거부한다. Idempotency-Key로 같은 작업을 조회한다.

기존 후보는 `POST /services/{serviceId}/repairs/{repairId}/auto`의 명시적인 AI 수정 클릭으로만
자동 게시/머지 권한을 얻는다. 기존 레코드와 페이지 조회는 절대 새 머지 권한을 얻지 않는다.
모델 generation의 RUNNING/SUCCEEDED와 publication의 QUEUED/PR_OPENED/WAITING_CHECKS/
RECOVERING/MERGED/ERROR/SKIPPED를 구분해 웹이 끝까지 polling한다.

서버 lifespan의 durable runner는 5초마다 승인된 작업을 이어간다. 페이지 종료·응답 유실·WAS
재시작 후에도 DB 기록과 에이전트 receipt로 같은 후보를 복구한다. 모델 제출 claim과 publication
row lock으로 중복 생성·동시 GitHub 쓰기를 막는다. 불확실한 결과에 모델을 다시 호출하지 않는다.
필수 GitHub 검사나 승인이 아직 충족되지 않으면 같은 PR을 자동 재시도한다. SHA 변경·권한 철회·
마감 초과는 오류로 중단한다. GitHub 보호 규칙을 우회하지 않는다. 머지 후에는 기존 배포 웹훅이
새 배포를 처리하며, MERGED는 배포 성공과 구분한다.

검증은 한 번의 동작으로 게시/머지, 옛 후보의 암묵적 머지 거부, 권한/HEAD 사전 거부, mode
멱등성, 필수 검사 대기 후 같은 PR 재사용, 브라우저 없는 runner, receipt 복구와 마감 중단을 포함한다.

## 실패 화면 진입과 설정 복구

실패 이력 행과 배포 상세 헤더에서 AI 수정·재배포를 바로 시작한다. 진단이 없으면
`diagnosisId`를 생략할 수 있고, 진단 ID와 처리 승인을 영속 기록에 먼저 저장해 진단→복구를 이어간다.
코드 변경은 기존 핫픽스/머지 후 merged SHA를 명시적으로 재배포한다. 이미 웹훅이 같은 SHA로
배포를 만들었다면 그 요청을 재사용한다. 서비스의 자동 배포 설정이 꺼져 있어도 사용자 클릭의
재배포 요청은 처리한다.

런타임 변수의 설정 계획은 코드 패치를 요구하지 않는다. 이미 저장된 값은 유지하여 재배포한다.
SESSION_SECRET이 없으면 마지막 성공 배포의 암호화된 스냅샷에서 기존 값을 복구한다. 성공 이력이
없는 최초 앱에서만 256-bit 무작위(64자 hex) 값을 만들어 기존 VariableService로 암호화 저장한다.
기존 값은 회전하지 않고, 값은 모델·Git·복구 결과·로그에 보내지 않는다. 없는 외부 API/DB 인증
정보는 생성하지 않으며 입력이 필요하다는 오류로 처리한다. 설정 복구 완료는 REDEPLOY_REQUESTED와
redeploymentId로 표시하고 실제 배포 성공과 구분한다.
