# 0001. Control API 가 GitHub OAuth·App 호출을 맡는다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
README 는 "Control API 는 외부 시스템 권한을 갖지 않는다"고 정한다. 취지는 인터넷에 열린 API 가 뚫려도 CodeBuild·GitOps 저장소·Argo CD 같은 배포 인프라를 건드리지 못하게 하는 것이다.
그런데 Railway 처럼 "GitHub 로그인 → 레포 고르기" 흐름을 만들려면 로그인 검증과 저장소·브랜치 조회가 GitHub 호출을 필요로 한다.

## 검토한 선택지
1. **Control API 가 GitHub 호출까지 한다.** 구조가 단순하고 일정에 맞다. API 서버에 GitHub App private key 가 들어간다.
2. **GitHub 호출을 별도 서버·Worker 로 옮긴다.** API 는 DB 만 다뤄 가장 엄격하다. 요청마다 비동기 왕복이 생기고 구조·일정 부담이 크다.

## 결정
선택지 1. "외부 시스템 권한"은 **배포 인프라(CodeBuild·GitOps·Argo CD) 권한**으로 해석하고, 사용자 로그인과 저장소 조회를 위한 GitHub 호출은 이 제약에 포함하지 않는다.

## 결과
- 배포 인프라 호출은 지금처럼 Worker 에서만 한다.
- API 서버에 `GITHUB_APP_PRIVATE_KEY` 가 들어가므로 App 권한은 Contents·Metadata **읽기 전용**으로 제한하고, 운영에서는 시크릿 저장소로 주입한다.
- 이 해석을 현겸님(설계 담당)과 공유한다. GitHub 까지 금지 범위라면 선택지 2 로 바꾸는 새 ADR 을 쓴다.
