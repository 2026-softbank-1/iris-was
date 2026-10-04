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

## 실패 화면 진입과 개발자 환경변수 설정

실패 이력 행과 배포 상세 헤더에서 AI 수정·재배포를 바로 시작한다. 진단이 없으면
`diagnosisId`를 생략할 수 있고 진단을 먼저 실행한다. 코드 변경만 핫픽스/머지 후 merged SHA를
명시적으로 재배포한다. 재배포는 이 수정 작업의 멱등성 키로만 재사용하며, 과거 실패 배포를
같은 SHA라는 이유로 재사용하지 않는다.

환경변수 설정 계획 또는 Zod의 `received undefined` 근거가 있으면 개발자 설정 필요로 처리한다.
이미 끝난 진단은 409 CONFIGURATION_VALUES_REQUIRED와 변수 이름만 포함한 details를 돌려준다.
진단 진행 중인 자동 작업은 configuration_required/variableNames 결과와 SKIPPED로 끝낸다.
기존에 대기하던 variables strategy도 값을 생성·복구·변경하거나 재배포하지 않고 같은 안내로 전환한다.
GitHub 쓰기 권한 검사와 수정 모델 호출보다 먼저 이 분기를 적용한다.

앱의 소스 기본값, PORT 및 IRIS_* 시스템 변수, 이미 저장된 사용자 변수는 유지한다.
웹 변수 추가 화면은 SESSION_SECRET 최초값으로 Web Crypto 256-bit 난수(64자 hex)를 제안한다.
기존 값이 있으면 그 값을 편집하고 새 기본값을 생성하지 않는다. 개발자가 저장하기 전까지
서버에는 반영하지 않는다. 외부 DB/API 인증 정보는 기본값을 만들지 않는다.
.env 업로드는 기존 변수와 합쳐 Raw 편집기에 표시하고 개발자가 검토·저장한다. 이후 직접
재배포해야 런타임에 반영된다. 비밀값은 코드 저장소·모델·복구 결과·로그에 기록하지 않는다.
