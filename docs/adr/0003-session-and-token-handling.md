# 0003. 세션은 서명된 JWT 로 하고 GitHub 사용자 토큰은 저장하지 않는다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
웹(브라우저)과 CLI 모두 로그인 상태가 필요하다. Notion 작업 순서의 `users` 테이블에는 GitHub access token 컬럼이 있다.

## 검토한 선택지
1. **세션 테이블 + 불투명 토큰.** 즉시 폐기가 쉽지만 테이블·정리 작업이 늘어난다.
2. **서명된 JWT(HS256).** 서버 저장소가 필요 없다. 만료 전 강제 폐기가 어렵다.
3. GitHub 사용자 토큰을 저장해 재사용 → 유출 시 피해가 크고 installation 토큰으로 충분하다.

## 결정
- 세션은 HS256 JWT. 웹은 httpOnly 쿠키(`anydeploy_session`), CLI 는 `Authorization: Bearer`. 기본 만료 7일.
- OAuth state 는 목적(`oauth_state`)이 다른 서명 JWT 와 nonce 쿠키로 CSRF 를 막는다.
- **GitHub 사용자 토큰은 로그인 중 한 번 쓰고 저장하지 않는다.** Notion 의 `users.access_token` 컬럼은 만들지 않는다.
- `SESSION_SECRET`·GitHub 설정이 없으면 해당 기능은 `503 NOT_CONFIGURED` 로 답한다.

## 결과
- DB 에 장기 자격 증명이 없다.
- 로그아웃은 쿠키 삭제뿐이라 탈취된 토큰은 만료까지 유효하다. 필요해지면 세션 테이블(선택지 1)로 대체한다.
