# 0014. 서비스 도메인은 저장하지 않고 타깃 접미사로 계산해 조회만 제공한다

- 상태: 수락됨
- 날짜: 2026-10-02
- 결정자: 김지민

## 배경
Notion task "[API] 도메인 발급·조회 API"는 타깃별 도메인을 만들고 저장하고 조회하는 것이었다. 처음에는 `service_domains` 테이블에 발급해 저장하고 Deploy Worker 가 그 값을 읽는 안으로 구현했다. 그 안은 서비스 이름을 바꿔도 주소를 유지하려는 것이었다.

그런데 서비스 이름 변경과 도메인 변경은 MVP 범위가 아니다. 이름이 바뀌지 않으면 `{이름}-{service_id}` 계산 결과도 변하지 않으므로 주소를 저장할 이유가 없다.

iris-infra 를 확인한 인프라 현황은 이렇다.

| 대상 | 상태 |
|---|---|
| DNS | `likelion.uk` 의 권한 DNS 는 Cloudflare 다. `*.likelion.uk` 가 workload ALB 로 이미 연결돼 있다 |
| 인증서 | ACM `*.likelion.uk`+apex. 와일드카드는 label 한 단계만 덮는다(`x.local.likelion.uk` 는 TLS 검증 실패를 확인) |
| AWS 서비스 | 서비스마다 필요한 것은 `values.yaml` 의 `route.host` 뿐이라 배포하면 `{이름}-{id}.likelion.uk` 로 열린다 |
| local 타깃 | 터널·공개 URL 이 iris-infra 에 아직 없다 |

## 검토한 선택지
1. 발급해 `service_domains` 에 저장하고 Deploy Worker 가 읽는다 — 이름을 바꿔도 주소가 유지된다. 테이블·백필·Worker 변경이 필요한데 MVP 에는 쓸모가 없다.
2. 저장하지 않고 계산해서 조회만 제공한다 — 변경이 작고 Worker 동작이 그대로다. 이름을 바꾸면 주소도 바뀐다.

## 결정
2 를 택한다.

- 주소는 `{서비스 이름}-{service_id}.{targets.domain_suffix}` 로 계산한다. 계산 함수(`build_service_host`)는 `app/services/domain_service.py` 한 곳에 두고, Deploy Worker 도 같은 함수로 label 을 만든다(`service_host_label` 을 옮겼을 뿐 동작은 같다).
- `GET /api/v1/services/{id}/domains` 가 연결한 타깃마다 `host`·`url`·`isConnected`(그 타깃에 `SUCCEEDED` release 가 있는지)를 준다. 접미사가 없는 타깃은 `host` 가 비어 있다.
- 접미사는 점 하나(`likelion.uk`)여야 와일드카드 인증서가 덮는다. revision `8c77b62ef71e` 가 `aws` 타깃의 접미사를 `likelion.uk` 로 정한다(이미 값이 있으면 건드리지 않는다).
- 발급·변경 API 는 만들지 않는다.

## 결과
- 프런트가 타깃별 주소와 연결 여부를 한 번에 받는다. Deploy Worker 와 스키마는 바뀌지 않는다.
- 주소 규칙의 접미사가 두 곳에 있다. API 는 `targets.domain_suffix`, Deploy Worker 는 `BASE_DOMAIN` 이다. 둘을 같은 값(`likelion.uk`)으로 둔다.
- `PATCH /services/{id}` 로 이름을 바꾸면 주소도 바뀐다. MVP 에서 이름 변경을 허용할지는 별도로 정한다.
- 이름 변경을 지원하게 되면 1번(발급해 저장)을 다시 검토한다.

### 남은 일 — local 타깃의 도메인 (인프라와 결정)
local 타깃은 `domain_suffix` 가 비어 있어 `host` 가 없다. 접미사를 정하면 코드는 그대로 주소를 계산한다. 정할 것은 인프라 쪽이다.

- `*.likelion.uk` 는 이미 AWS ALB 로 가므로 local 주소는 이름이 겹치지 않아야 하고, 터널로 가려면 그 호스트 전용 DNS 레코드가 필요하다(구체적인 레코드가 와일드카드보다 우선한다).
- 두 단계 와일드카드(`*.local.likelion.uk`)는 Cloudflare 기본 인증서가 덮지 않아 별도 인증서가 필요하다.
- DNS 레코드·터널을 누가 만드는지(Cloudflare 자격증명을 어느 컴포넌트가 갖는지)는 "컴포넌트끼리 자격증명을 공유하지 않는다" 원칙과 함께 정해야 한다.
