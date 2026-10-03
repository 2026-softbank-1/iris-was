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
| [0010](0010-deployment-status-transitions-and-history.md) | 배포 요청 상태는 전이 함수로만 바꾸고 전이마다 이력을 남긴다 | 수락됨 |
| [0013](0013-integrate-build-deploy-workers-on-develop-models.md) | main 의 Build·Deploy Worker 를 develop 모델 위에 통합하고 옛 마이그레이션을 걷어낸다 | 수락됨 (현겸님 확인 대기) |
| [0014](0014-service-domain-lookup.md) | 서비스 도메인은 저장하지 않고 타깃 접미사로 계산해 조회만 제공한다 | 수락됨 |
| [0015](0015-rollback-and-restart-reuse-built-image.md) | 롤백과 재시작은 이미 빌드한 이미지를 다시 배포하는 요청으로 만든다 | 수락됨 |
| [0016](0016-remove-service-deployment.md) | 서비스 삭제는 GitOps 디렉터리를 지워 ApplicationSet 이 Application 을 정리하게 한다 | 수락됨 (인프라 적용 대기) |
| [0017](0017-service-variables-encrypted-storage-and-deploy-snapshot.md) | 서비스 환경변수는 암호화해 DB 에 저장하고 배포 요청마다 스냅샷을 남긴다 | 제안됨 |
| [0018](0018-cli-login-session-table-and-polling.md) | CLI 로그인은 DB 세션 테이블과 폴링으로 하고 토큰은 한 번만 내준다 | 수락됨 |
| [0019](0019-job-wakeup-listen-notify.md) | Worker 는 5초 polling 대신 jobs 트리거의 LISTEN/NOTIFY 로 깨운다 | 제안됨 |
| [0020](0020-ai-error-diagnosis-via-agent-server.md) | 실패한 배포의 AI 진단은 Control API 가 에이전트 서버를 호출하고 결과를 DB 에 저장한다 | 제안됨 (에이전트 서버 배포 대기) |
