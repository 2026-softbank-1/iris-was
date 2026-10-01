# 0007. API 문서는 FastAPI 가 만드는 OpenAPI(Swagger)로 하고 테스트로 최신 상태를 강제한다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
프론트·CLI 담당과 심사위원이 API 를 볼 문서가 필요하다. 해커톤에서는 문서를 손으로 쓰면 코드와 어긋나기 쉽고, 쓸 시간도 부족하다.

## 검토한 선택지
1. **Notion·마크다운에 API 표를 직접 관리.** 자유롭지만 코드와 어긋난다.
2. **FastAPI 자동 생성 OpenAPI(`/docs`·`/redoc`·`/openapi.json`).** 코드가 곧 문서라 어긋나지 않는다. 설명·에러 응답은 코드에 적어야 한다.
3. **Postman 컬렉션 등 별도 도구.** 팀 공유 비용이 따로 든다.

## 결정
선택지 2. 다음을 규칙으로 한다.
- 엔드포인트마다 `summary`, 라우터 `tags`, 에러 응답(`error_responses(...)`)을 선언한다. 인증이 필요하면 401, 입력이 있으면 422 를 포함한다(422 는 기본 `HTTPValidationError` 가 아니라 `ApiResponse` 봉투로 문서화한다).
- 인증 방식(쿠키·Bearer)을 명세에 선언해 Swagger 의 Authorize 로 호출해 볼 수 있게 한다.
- 명세는 `docs/openapi.json` 으로 내보내 저장소에 커밋한다(`uv run python -m scripts.export_openapi`). 프론트·CLI 는 이 파일로 타입·클라이언트를 만들 수 있다.
- `tests/test_openapi.py` 가 위 규칙과 내보낸 파일의 최신 여부를 검사한다. 규칙을 어기거나 파일이 낡으면 테스트가 실패한다.

## 결과
- 문서화를 빼먹으면 CI(테스트)에서 막힌다.
- 엔드포인트를 바꿀 때마다 `docs/openapi.json` 갱신이 diff 에 포함된다.
- 별도 UI(Scalar 등)는 필요해질 때 같은 명세로 추가한다.
