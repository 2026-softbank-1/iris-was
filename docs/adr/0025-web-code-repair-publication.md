# 0025: 웹의 AI 수정은 WAS에서 핫픽스 PR 게시와 머지를 별도 실행한다

상태: 제안됨

기존 후보 생성 API와 CLI 코디네이터만으로는 웹에서 AI 수정 버튼을 실행할 수 없다. 웹은 진단 계획 중 모든 변경이 `kind=code`인 계획을 선택하고 기존 후보 생성 API를 호출한다. 설정·명령 변경 계획은 진단 안내로 남긴다.

- `GET /api/v1/services/{service_id}/repair-access`: 기존 WAS 세션·서비스 소유권·설치 저장소 접근을 확인하고 Contents/Pull requests write 단기 토큰을 발급해 버린다. 브라우저에는 `canWrite`, 저장소, 설치 승인 URL만 반환한다. 권한 부족도 정상 응답으로 안내하고 서버/설정 문제는 오류로 반환한다. `Cache-Control: no-store`를 적용한다.
- `GET /services/{service_id}/deployments/{deployment_id}/repairs/latest?diagnosisId=...`: 특정 진단의 최근 후보 작업을 조회한다. 웹은 이를 통해 새로고침 후 이어서 본다. 생성 POST는 Idempotency-Key를 재사용한다.
- `POST /services/{service_id}/repairs/{repair_id}/publish`: 봉인된 `changes.json`, `manifest.json`, `patch.diff`의 무결성과 후보 digest, 고정 source SHA, 서비스 root를 검증한다. 사용자 설치 토큰은 서버 메모리에만 둔다. 서비스의 현재 저장소가 원본과 같고 브랜치가 main이어야 한다. `hotfix/iris/was-repair-{id}`를 생성하고 main 대상 일반 PR을 열되 머지하지 않는다.
- `POST /services/{service_id}/repairs/{repair_id}/merge`: 사용자가 검토한 PR을 명시적으로 머지한다. 저장소·base/head·고정 후보 SHA를 확인하고, main이 원본 실패 commit과 같아야 한다. GitHub 필수 검사·승인·보호 규칙을 우회하지 않는다. 이미 머지된 PR은 먼저 조회해 응답 유실을 복구한다.

게시 상태와 공개 commit/PR 식별자는 기존 `request_metadata.publication`에 저장하고 `RepairResponse.publication`으로 반환한다. 후보 결과 자체는 변경하지 않는다. 스키마 마이그레이션은 없다. 게시/머지 동안 repair row의 PostgreSQL `FOR UPDATE NOWAIT` 잠금을 유지해 WAS replica 간 중복 쓰기를 막는다. 네트워크 오류에도 알고 있는 commit/PR 식별자를 보존하고 같은 작업을 재시도한다. 프로세스 종료 시 트랜잭션은 롤백되지만 동일 후보·메시지·원본 부모·기록 시각으로 같은 commit을 재생성하고 ref/PR을 조회해 복구한다.

GitHub 게시 클라이언트는 [iris-code-fix-agent PR #1](https://github.com/2026-softbank-1/iris-code-fix-agent/pull/1)의 publication 구현과 테스트를 WAS 타입/예외 규약에 맞춰 옮겼다. 소스/보호 경로·원본 파일 hash·원본 Git tree 보존 규칙을 유지한다. 향후 두 구현을 함께 갱신해야 한다.

운영 적용 순서는 WAS 배포 → 웹 배포다. 기존 `REPAIR_AGENT_*`, 소스 스냅샷, App 설정을 재사용한다. App 관리자는 Contents와 Pull requests를 Read and write로 설정하고 설치 소유자가 새 권한을 승인해야 한다. 로그인 성공만으로 쓰기 권한이 생기지 않는다. 머지 완료는 배포 성공이 아니며 기존 서비스 웹훅과 자동 배포 설정에 따라 새 배포가 진행된다.

검증은 후보 무결성 거부, 소유권, 권한 거부 후 같은 후보 재사용, PR/merge 멱등성, 변경된 main 거부, 응답 유실 복구, 토큰 비노출과 PostgreSQL replica 잠금을 포함한다. 브라우저 QA는 가짜 API로 계획 필터·권한 안내·생성·패치 검토·명시적 PR 게시/머지·새로고침 복구를 검증했다. 실제 GitHub 수정과 운영 배포는 별도다.
