# 0036. GCP(GKE) 를 공용 배포 타깃으로 더한다

- 상태: 채택
- 날짜: 2026-10-04

## 배경

iris-infra 가 GKE dev workload(`gcp-dev-workload`)를 만들고 management Argo CD 에 붙였다(iris-infra
ADR 0007, `contracts/gcp-target.md`). 디렉터리 `services/{id}/gcp` 를 ApplicationSet
`iris-svc-gcp-appset` 이 `gcp-svc-{id}` Application(project `iris-svc-gcp-project`, chart
`iris-service-0.10.0`)으로 만든다. 트래픽은 `*.gcp.likelion.uk` → GCP Gateway 로 바로 간다.

## 결정

- 타깃 종류 `GCP` 와 공용 타깃 행 `gcp`(도메인 `gcp.likelion.uk`)를 더한다. 사용자는 AWS·on-prem 처럼
  서비스 타깃으로 고른다. 서비스 주소는 `{서비스 이름}-{service_id}.gcp.likelion.uk` 다.
- Deploy Worker 는 GCP release 를 `services/{id}/gcp/values.yaml` 에 쓰고, Application 이름은
  `gcp-svc-{id}` 다. project 가 달라 읽기 토큰을 따로 받는다(`ARGOCD_GCP_TOKEN`, role
  `iris-deploy-reader`).
- 서비스 변수는 GCP 클러스터의 Sealed Secrets 공개 인증서(`GCP_SEALED_SECRETS_CERT`)로 봉인한다.
- chart 0.10.0 의 GCP 경로는 Deployment·롤링만 받고 DB 를 거절한다. 그래서 GCP 는 on-prem 처럼
  배포 방식은 `ROLLING` 만, 관리형 DB·호스트 별칭·참조 변수(프로젝트 내부 통신)는 쓰지 않는다.
  ECR pull Secret·Gateway·NetworkPolicy 는 ApplicationSet 이 넣으므로 values 에 쓰지 않는다.
- 서비스 콘솔·런타임 로그·메트릭은 아직 GCP 를 읽지 않는다(콘솔은 `TARGET_NOT_SUPPORTED`).

## 결과

- 배포·롤백(revert commit)·삭제(디렉터리 제거)는 기존 흐름 그대로 GCP 에도 된다.
- GCP 앱의 health 경로는 인증 없이 HTTP 200 이어야 한다(GCP LB health check). AWS ALB 의 200–499 와 다르다.
- 관리 Argo CD·ECR 은 AWS 에 남아 있어, 그 장애는 GCP 의 새 배포·pull 에도 영향을 준다.
