# 아키텍처 결정 기록 (ADR)

중요한 설계 결정을 **왜 그렇게 정했는지**와 함께 남기는 문서다. 형식은 [MADR](https://adr.github.io/madr/)(Markdown ADR)을 간추려 쓴다.

## 규칙

- 결정 하나에 파일 하나. 파일명은 `NNNN-짧은-제목.md`(번호는 순서대로, 재사용하지 않는다).
- 한 번 `수락됨`이 된 ADR 은 본문을 고치지 않는다. 결정이 바뀌면 새 ADR 을 쓰고, 옛 ADR 의 상태를 `대체됨(NNNN)` 으로 바꾼다.
- 상태: `제안됨` → `수락됨` → (`폐기됨` | `대체됨(NNNN)`)
- 코드·스키마를 바꾸는 결정이면 PR 과 함께 ADR 을 추가한다. 용어·규칙 문서와 충돌하면 그 문서 갱신도 같이 한다.

## 템플릿

```markdown
# NNNN. 제목 (결정을 한 문장으로)

- 상태: 제안됨 | 수락됨 | 폐기됨 | 대체됨(NNNN)
- 날짜: YYYY-MM-DD
- 결정자: 이름

## 배경
어떤 문제·제약 때문에 결정이 필요했는가.

## 검토한 선택지
1. 선택지 A — 장점 / 단점
2. 선택지 B — 장점 / 단점

## 결정
무엇을 택했고, 왜 택했는가.

## 결과
좋아지는 점, 감수하는 점, 후속 작업.
```

## 목록

| 번호 | 제목 | 상태 |
|---|---|---|
| [0001](0001-control-api-calls-github.md) | Control API 가 GitHub OAuth·App 호출을 맡는다 | 수락됨 |
| [0002](0002-single-github-app-for-login-and-repo-access.md) | GitHub App 하나로 로그인과 저장소 접근을 처리한다 | 수락됨 |
| [0003](0003-session-and-token-handling.md) | 세션은 서명된 JWT 로 하고 GitHub 사용자 토큰은 저장하지 않는다 | 수락됨 |
| [0004](0004-domain-model-reconciliation.md) | 용어 사전의 핵심 엔티티를 유지하고 Railway 방식 엔티티를 추가한다 | 수락됨 |
| [0005](0005-soft-delete-and-deferred-cleanup.md) | 프로젝트·서비스 삭제는 소프트 삭제로 하고 리소스 정리는 미룬다 | 수락됨 |
| [0006](0006-service-creation-defaults.md) | 서비스 생성 기본값과 이름 규칙 | 수락됨 |
| [0007](0007-api-documentation-with-openapi.md) | API 문서는 OpenAPI(Swagger)로 하고 테스트로 최신 상태를 강제한다 | 수락됨 |
| [0008](0008-branch-strategy.md) | 브랜치 전략: develop 통합, release 에서 검증 후 main 병합 | 수락됨 |
| [0009](0009-github-webhook-receiver.md) | GitHub 웹훅은 서명으로 인증하고 push 를 배포 요청으로 바꾼다 | 수락됨 |
