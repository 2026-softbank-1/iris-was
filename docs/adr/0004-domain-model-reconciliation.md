# 0004. 용어 사전의 핵심 엔티티를 유지하고 Railway 방식 엔티티를 추가한다

- 상태: 수락됨
- 날짜: 2026-10-01
- 결정자: 김지민

## 배경
용어 사전(최우선 규칙)은 `services`·`deployment_requests`·`jobs`·`builds`·`releases` 를 정의한다. Notion 작업 순서는 Railway 방식(프로젝트, 사용자, 배포 대상 등)의 모델을 가정한다. 둘이 완전히 같지 않다.

## 검토한 선택지
1. Notion 모델로 처음부터 다시 설계 → 현겸님 엔진 작업과 충돌.
2. 용어 사전만 따르고 부족한 부분은 구현하지 않음 → 인증·프로젝트 기능이 불가능.
3. 용어 사전을 유지하고 필요한 엔티티를 추가(`*` 표기) 후 합의.

## 결정
선택지 3. 추가한 테이블: `users`, `github_installations`, `user_github_installations`, `projects`, `targets`, `service_targets`.
- 테이블명은 snake_case 복수형(용어 사전이 단수 규칙보다 우선).
- 서비스 계층의 Service 도메인 클래스는 `ServiceRegistryService`.
- 빌드는 요청당 1건, 릴리스는 타깃마다 1건(같은 이미지를 타깃마다 한 번씩 배포).
- 타깃 `aws`·`local` 은 마이그레이션으로 시드한다.
- 환경변수·도메인·배포 단계 테이블은 해당 단계에서 별도 revision 으로 추가한다.

## 결과
- 스키마 변경은 모델 + Alembic revision + `db-schema.sql` + 용어 사전을 한 세트로 갱신했다.
- 현겸님이 용어 사전의 `*` 항목을 검토·확정해야 한다.
