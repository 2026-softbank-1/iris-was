# 0009. GitHub 웹훅은 서명으로 인증하고 push 를 배포 요청으로 바꾼다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
브랜치에 push 하면 자동으로 배포하려면(`is_auto_deploy`) GitHub 가 보내는 웹훅을 받아야 한다. 이 API 는 로그인한 사용자가 아니라 GitHub 가 호출하므로 세션 인증을 쓸 수 없고, 같은 전송이 다시 오거나(재전송) 동시에 올 수 있다.

## 결정
- `POST /api/v1/webhooks/github` 하나로 모든 이벤트를 받는다. 인증은 `X-Hub-Signature-256` 을 본문 원문의 HMAC-SHA256 으로 검증한다(`GITHUB_WEBHOOK_SECRET`, 상수 시간 비교). 서명이 없거나 틀리면 `401`, 시크릿 설정이 없으면 `503 NOT_CONFIGURED` 다.
- 처리하는 이벤트
  - `push`: 브랜치 갱신만 본다(태그·브랜치 삭제는 무시). 저장소 주소(대소문자 무시)·브랜치가 같고 `is_auto_deploy` 인 서비스마다 배포 요청(`trigger_type=PUSH`, `requested_by` 없음)과 BUILD job 을 만든다.
  - `installation`: `created` 는 설치 정보를 등록·갱신하고, `deleted` 는 사용자와의 연결만 끊는다(서비스가 설치 행을 참조하고 이력이 남아야 해서 행은 지우지 않는다). 나머지 action 과 다른 이벤트는 `200` 으로 받고 무시한다. GitHub 가 실패로 보고 재전송하지 않게 하기 위해서다.
- 멱등성: 키는 `github-push:{X-GitHub-Delivery}:{service_id}` 다. 같은 delivery 의 재전송은 같은 서비스에 요청을 만들지 않는다.
- 진행 중인 배포(`QUEUED`·`BUILDING`·`DEPLOYING`)가 있는 서비스의 push 는 **건너뛴다**(대기열에 쌓거나 덮어쓰지 않는다). 서비스·환경당 진행 중 요청은 하나라는 기존 제약을 따른다. 건너뛴 push 의 커밋은 다음 push 때 함께 배포된다.
  - 중복·진행 중 판정은 `INSERT ... ON CONFLICT DO NOTHING` 에 맡겨, 동시에 온 웹훅이 제약을 두고 경쟁해도 트랜잭션이 깨지지 않는다.
- 모노레포: 서비스에 `root_directory` 가 있으면 push 의 변경 파일이 그 디렉터리 아래일 때만 배포한다. 변경 파일을 알 수 없으면(commits 가 비었거나 GitHub 가 싣는 한도인 20개 이상) 배포한다.
- 빌더가 아직 없는 서비스(코드 분석 전)도 요청은 만든다. "빌더 확정 전에는 배포하지 않는다"(ADR 0006)는 Build Worker 가 `BUILD_CONFIG_REQUIRED` 로 실패시키는 단계에서 지킨다. 분석이 구현되기 전에는 push 요청이 모두 이 코드로 끝난다.
- 배포 요청과 첫 BUILD job 의 생성은 `DeploymentRequestService` 가 맡는다. 이후 수동 배포 API 도 같은 서비스를 쓴다. job payload 는 요청 시점의 소스 위치(`BuildJobPayload`)를 고정해 둔다.

## 결과
- push 만으로 배포 요청이 생기고, 재전송·동시 전송에도 중복되지 않는다.
- 연속 push 를 건너뛰므로 가장 마지막 커밋이 배포되지 않을 수 있다. 진행 중인 배포가 끝난 뒤 새 push 나 수동 재배포가 필요하다. 문제가 되면 "진행 중 요청을 취소하고 최신으로 교체"하는 정책을 새 ADR 로 정한다.
- 로컬에서 받으려면 터널(smee.io·cloudflared)과 dev 앱의 웹훅 활성화·Secret 설정이 필요하다(README).
- 후속 작업: 설치 `suspend`·`unsuspend` 반영(스키마 필요), 서비스·설치의 일치 검증.
