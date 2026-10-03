# 0002. GitHub App 하나로 로그인과 저장소 접근을 처리한다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
사용자 로그인과 레포 접근(목록·브랜치·이후 빌드용 clone)이 모두 필요하다. GitHub 는 OAuth App 과 GitHub App 두 방식을 제공한다.

## 검토한 선택지
1. **OAuth App (사용자 토큰으로 레포 접근).** 설정이 쉽지만 토큰 권한이 넓고(`repo` 스코프), 사용자 토큰을 오래 보관해야 한다.
2. **OAuth App + GitHub App 둘 다.** 역할은 분명하나 앱 두 개를 관리한다.
3. **GitHub App 하나.** 로그인은 user authorization, 레포 접근은 installation 토큰(단기·저장소 단위)으로 처리한다.

## 결정
선택지 3. 설치 직후 로그인으로 이어지도록 "Request user authorization (OAuth) during installation"을 켜고, 콜백은 `/api/v1/auth/github/callback` 하나를 쓴다.
- 로그인 때마다 `/user/installations` 로 `user_github_installations` 를 동기화한다.
- 저장소가 속한 설치는 **저장소 소유자 = 설치 계정(account_login)** 으로 고른다.
- 접근 권한이 없으면 `403 REPOSITORY_NOT_ACCESSIBLE` 로 설치를 안내한다.

## 결과
- 사용자가 허용한 저장소만 보이고 토큰은 1시간 단기 토큰이다.
- 빌드 쪽은 `create_clone_token(installation_id)` 로 clone 토큰을 받는다.
- 설치 상태가 바뀐 직후에는 다음 로그인 전까지 목록이 어긋날 수 있다. 웹훅 동기화는 후속 과제다.


## 코드수정 인증 연동

로그인 콜백의 사용자 OAuth 토큰은 사용자·설치 목록 조회에만 쓰고 저장하지 않는다.
코드수정 코디네이터는 기존 WAS 세션으로 인증하고
`POST /api/v1/services/{serviceId}/repair-github-token` 으로 서비스용 단기 설치 토큰을 받는다.
WAS 는 서비스 소유권과 source repository 일치 여부, 사용자와 설치의 연결, 현재 저장소 접근권한을
검사한 뒤 그 저장소 하나에 `contents: write`, `pull_requests: write` 를 요청한다.
GitHub App 과 설치에 두 쓰기 권한이 허용되어야 한다. GitOps App 자격증명은 쓰지 않는다.

요청은 `{"repository":"owner/repo"}`, 응답 data 는 `repository`, `token`, `expiresAt` 이다.
응답에는 `Cache-Control: no-store` 를 설정한다. 토큰은 신뢰된 코디네이터 메모리에서만 사용하고
수정 생성 API·작업 기록·로그에는 보내지 않는다. 코디네이터가 만료 60초 전에 WAS 에서 갱신한다.
서비스 소유자가 아니면 404, source repository 가 바뀌었으면 409, 저장소·쓰기 권한이 없으면 403 이다.
WAS 세션 만료는 기존 로그인으로, 설치·권한 변경은 기존 `/api/v1/github/install` 흐름으로 처리한다.
