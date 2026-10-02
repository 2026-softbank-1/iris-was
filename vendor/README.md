# 고정 분석 패키지

`iris_analyzer-0.1.0-py3-none-any.whl`은 팀의 비공개 분석기 저장소
`2026-softbank-1/iris-code-analyzer-agent` 커밋
`1d9d2e38086b394d60fe90d889c6978357e35681`의 clean checkout에서 생성했다.
WAS CI·Docker가 별도 저장소 토큰을 요구하지 않도록 검증된 wheel을 함께 보관한다.

`analyzer-manifest.json`은 원본 repository/commit, wheel SHA-256 및 포함된 package 파일의
digest를 기록한다. 생성 시 모든 package 파일을 해당 source checkout과 바이트 단위로 대조했다.
`.env`, 모델 요청·응답 기록과 기타 작업 산출물은 포함하지 않는다.

분석기 변경 시 원본 코드의 검증·검토가 끝난 커밋에서 wheel을 재생성하고 manifest·uv.lock을
함께 갱신한다. 이 버전의 정적 분석·스냅샷·검증 계약이 Worker와 일치하는지 통합 테스트를
다시 실행한다. wheel을 직접 수정하지 않는다.

실패 로그 진단은 `ai_error_check_agent-0.2.0-py3-none-any.whl`을 사용한다.
원본 `2026-softbank-1/iris-error-check-agent`의 clean commit
`e4d1984bc1b0770623f4da02243100ab8639e638`이며, `diagnosis-manifest.json`이 wheel과
package 파일 digest를 기록한다. 실제 upstream diagnosis-request.v1/result.v2 검증과
OpenAI runtime을 재사용한다. 변경·유료 모델 호출·수정 실행은 wheel 생성에 포함하지 않는다.
