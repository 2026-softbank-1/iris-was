# 0033. 서비스 콘솔은 Console Gateway 가 `pods/exec` 를 중계하고, Control API 는 서명한 ticket 만 발급한다

- 상태: 제안됨 (iris-infra 의 Gateway·Prod RBAC 적용과 E2E 대기. AWS 타깃만, 온프레미스는 다음 결정)
- 날짜: 2026-10-04
- 결정자: 김지민

## 배경
서비스 화면(`/project/{projectId}/service/{serviceId}/console`)의 콘솔은 목업이다. 사용자가 실행 중인 레플리카(Pod)의 `app` 컨테이너에서 셸을 열 수 있게 하고 싶다. 쿠버네티스 `pods/exec`(WebSocket)는 EKS 와 K3s 가 똑같이 지원하므로 기술적 장벽은 없다. 막는 것은 지금의 권한 설계다.

- Control API 는 클러스터 접근이 금지다(흐름 문서 §3). Prod 는 Argo CD 만 접근한다(§7).
- Argo CD 내장 터미널은 꺼져 있다(iris-infra `helm/bootstrap/values.yaml`, `"exec.enabled": "false"`). 서비스 배포용 Argo ServiceAccount 에는 `pods/exec` 가 없고, 온프레미스 쪽은 Secret 까지 읽는 전체 읽기 권한이다.
- 이 서버에는 WebSocket 이 없다(SSE 만 있다). 로그·메트릭은 Loki·Prometheus 를 거쳐 클러스터에 닿지 않는다.
- 사용자 Pod 는 SA 토큰을 마운트하지 않고, NetworkPolicy·PSA baseline 이 적용된다(`deploy-worker-plan.md`). 사용자 코드가 이미 그 Pod 안에서 돌고 있으므로, exec 가 새로 여는 권한은 거의 없다.

## 검토한 선택지
연결 방식
1. **Control API 가 직접 exec.** 가장 단순하지만 Control API 가 Prod 클러스터 자격증명을 갖게 되어 "Control API 는 클러스터 접근 금지"가 무너진다. 사용자 요청을 받는 가장 넓은 면에 클러스터 권한이 붙는다.
2. **Argo CD 터미널을 중계.** `exec.enabled` 를 켜고 Control API 가 Argo 의 `/terminal` 을 중계한다. 코드가 가장 적다. 하지만 `/terminal` 은 UI 내부 엔드포인트라 안정 API 가 아니고, 이미 넓은 Argo 권한에 exec 가 얹히며, Control API 가 Argo CD 를 부르게 된다(ADR 0029 의 경계).
3. **별도 컴포넌트 Console Gateway.** 사용자 인증·소유권 확인은 Control API 가 하고(ticket 발급), 클러스터 접근은 `pods/exec` 만 가진 Gateway 가 한다. 컴포넌트가 하나 늘지만 기존 경계(Build·Deploy Worker 처럼 같은 이미지에 실행 명령만 다르다)를 그대로 쓴다.
4. **클러스터 안 에이전트 + 역방향 터널.** 인바운드가 없는 온프레미스에는 잘 맞지만, AWS 에는 불필요한 구성이고 설치·보안 면적이 크다.

ticket 전달
- **URL 쿼리**: 브라우저 WebSocket 이 Authorization 헤더를 못 보내서 흔히 쓰지만, ALB 접근 로그·프록시 로그에 토큰이 남는다.
- **쿠키**: Gateway 가 세션 서명 비밀을 알아야 해서 컴포넌트끼리 비밀을 공유하게 된다.
- **연결 후 첫 프레임(`auth`)**: URL·로그에 비밀이 남지 않는다. 5초 안에 오지 않으면 끊는다.

ticket 서명
- **HS256 공유 비밀**: Gateway 가 서명도 할 수 있어 키가 새면 ticket 을 위조한다. 비밀을 둘이 공유한다.
- **Ed25519 비대칭**: Control API 만 개인키를, Gateway 는 공개키만 가진다. Gateway 가 털려도 ticket 을 만들지 못한다. 이미 있는 `pyjwt[crypto]` 로 된다.

ticket 1회용 보장
- **Gateway 메모리**: DB·공유 저장소가 필요 없다. Gateway 를 한 replica 로 둬야 정확하다.
- **DB(`console_sessions`)에서 소비 표시**: Gateway 가 DB 접속 정보를 갖게 되어 Gateway 의 경계가 커진다. 거절한다.
- **Control API 에 소비 콜백**: Gateway→Control API 호출과 서비스 간 인증이 새로 필요하다.

K8s 접속 라이브러리
- **`kubernetes-asyncio`**: 의존성이 크고 EKS IAM 토큰(`k8s-aws-v1.`)은 어차피 직접 만들어야 한다. 우리가 쓰는 API 는 Pod 목록·조회와 exec 둘뿐이다.
- **`httpx` + `websockets`**: Pod 조회는 이미 쓰는 `httpx`, exec 는 `v4.channel.k8s.io` 프로토콜을 `websockets` 클라이언트로 직접 다룬다. `websockets` 는 uvicorn 이 WebSocket 을 서버로 받는 데도 필요해(현재 `uvicorn` 은 `standard` extra 가 아니다) 의존성이 하나만 는다.

## 결정
3 을 택한다. ticket 은 연결 후 첫 프레임으로 전달하고, Ed25519 로 서명하며, 1회용은 Gateway 메모리로 막는다(replica 1). K8s 는 `httpx` + `websockets` 로 직접 접속한다. 범위는 AWS 타깃(Prod EKS)이다.

- **컴포넌트**: Console Gateway(`app/console_gateway/`)는 같은 이미지에서 `uvicorn app.console_gateway.main:app --host 0.0.0.0 --port 8080` 으로 띄운다. 자체 설정 클래스(`ConsoleGatewaySettings`)를 쓰므로 `DATABASE_URL` 이 필요 없고, DB 접속 정보를 갖지 않는다. `/healthz`·`/readyz` 는 204 다(`/readyz` 는 설정이 로드됐는지만 본다. 클러스터 장애로 Pod 가 빠지지 않게 클러스터 호출을 하지 않는다). Gateway 는 `app/routers/`·`app/workers/` 를 import 하지 않는다.
- **권한 경계**: Control API 에 더해지는 것은 ticket 서명용 Ed25519 **개인키**(`CONSOLE_TICKET_PRIVATE_KEY`)뿐이다. 클러스터 권한은 없다. Gateway 는 **공개키**(`CONSOLE_TICKET_PUBLIC_KEY`)와 Prod EKS 의 IRSA Role 만 갖는다. EKS access entry → Kubernetes group `iris-console` → ClusterRole `iris-console-exec`(`pods` get·list, `pods/exec` create + get)이고, Secret·Deployment 등은 읽지 못한다. 컴포넌트끼리 Secret·IAM Role 을 공유하지 않는다.
- **Control API**(저장·읽기만, 클러스터를 부르지 않는다)
  - `GET /api/v1/services/{serviceId}/console?targetId=` — 콘솔을 열 수 있는지. `available=false` 의 `reason` 은 `TARGET_NOT_SUPPORTED`(ONPREM)·`NOT_CONFIGURED`(설정 없음)·`NO_RUNNING_DEPLOYMENT`(그 타깃에 정상 release 가 없음, lastKnownGood 규칙 재사용) 순서로 판정한다.
  - `POST /api/v1/services/{serviceId}/console/sessions` → 201. 프로젝트 소유자만, 서비스에 연결된 타깃만. 같은 판정을 409(`CONSOLE_TARGET_NOT_SUPPORTED`·`NO_RUNNING_DEPLOYMENT`)·503(`NOT_CONFIGURED`)로 돌려주고, 통과하면 `console_sessions` 행(감사)을 남기고 ticket 을 발급한다. 서비스·타깃이 없거나 연결돼 있지 않으면 404 다(관측 API 는 422 로 돌려주지만 이 계약은 404 다).
- **ticket**: EdDSA JWT. `iss=iris-control-api`, `aud=iris-console-gateway`, `sub`(user_id), `jti`(=`sessionId`), `iat`, `exp`(60초 이하), `svc`, `tid`, `ns=svc-{svc}`, `cluster=aws`. Gateway 는 서명·iss·aud·exp 와 `ns == svc-{svc}` 를 검사한다. `GET /v1/pods` 는 만료 전까지 여러 번, WebSocket 연결은 `jti` 당 1번만 쓸 수 있다(Gateway 가 `exp` 이후 짧은 여유까지 메모리에 보관). 화면은 Pod 목록용·연결용으로 매번 새 ticket 을 받는다.
- **Gateway REST**: `GET /v1/pods`(Bearer ticket) → 그 namespace 의 Pod 중 컨테이너 `app` 이 있는 것. `releaseId` 는 Pod 라벨 `iris/release-id` 에서 읽는다(Deploy Worker 가 chart 로 넣는다). 허용 Origin 만 CORS 로 연다.
- **Gateway WebSocket**: `GET /v1/exec?pod=`. Origin 이 허용 목록에 없으면 업그레이드를 거절한다(403). 첫 프레임이 `{"type":"auth","token","cols","rows"}`. 프레임은 모두 JSON 텍스트다.
  - 클라이언트→서버: `auth`·`input`·`resize`·`ping`. 서버→클라이언트: `ready`·`output`·`pong`·`exit`·`error`. `error` 뒤에는 Gateway 가 소켓을 닫고 화면은 숫자 close code 가 아니라 `error.code` 로 분기한다.
  - namespace·컨테이너는 ticket 으로 고정된다(`svc-{svc}`·`app`). 요청에서 받지 않는다.
  - 셸은 먼저 `["/bin/sh","-c","command -v bash …"]` 로 한 번 탐색해 `bash` 가 있으면 bash 아니면 sh 를 쓰고(`ready.shell`), `/bin/sh` 자체가 없으면(distroless 등) `SHELL_NOT_FOUND` 다. 본 세션은 `TERM=xterm-256color`, `tty=true`, stdin 켬으로 연다.
  - 제한: 입력 없이 15분(`ping`·`pong` 은 입력이 아니다) → `IDLE_TIMEOUT`, 연결 후 1시간 → `MAX_DURATION_EXCEEDED`, 사용자당 동시 3개 → `SESSION_LIMIT_EXCEEDED`. 클라이언트가 25초마다 `ping`, Gateway 도 25초마다 WebSocket ping 을 보낸다(ALB idle timeout 60초 대비). 입력 프레임은 64KiB 까지다.
  - 감사: Gateway 가 `console_session_started`·`console_session_ended` 를 구조화 로그로 남긴다(`session_id`·`user_id`·`service_id`·`pod`·종료 사유·지속 시간). 셸 입출력은 어디에도 남기지 않는다.
- **클러스터 인증**: Gateway 가 EKS Pod Identity 자격증명(boto3 기본 자격증명 체인)으로 `sts:GetCallerIdentity` presigned URL(헤더 `x-k8s-aws-id: <cluster name>`)을 만들어 `k8s-aws-v1.<base64url>` bearer 토큰으로 쓴다. 15분 만료 전에 갱신한다. 이 호출은 `app/clients/aws_clients.py` 한곳에 둔다.
- **클러스터 클라이언트 선택**: Gateway 는 ticket 의 `cluster` 로 클러스터 Client 를 고른다. 지금은 `aws` 하나이고, 모르는 값은 `CLUSTER_UNAVAILABLE` 이다. 온프레미스는 이 자리에 Client 를 더하는 것으로 확장한다.

## 결과
- 사용자는 서비스 화면에서 AWS 타깃의 Pod 에 셸을 열 수 있고, Control API 의 권한은 Ed25519 개인키 하나만 늘어난다. 클러스터 접근은 새 컴포넌트가 `pods`(get·list)·`pods/exec`(create + get)만 가진다.
- **Gateway replica 는 1 이다.** 1회용 검사가 메모리 기반이라, replica 가 둘이 되면 같은 ticket 을 다른 replica 에 60초 안에 다시 쓸 수 있다. 서명·만료·소유권 검사는 그대로라 피해는 "60초 안에 같은 사용자가 같은 서비스에 연결을 한 번 더 여는 것"에 그친다. 늘려야 하면 sticky 라우팅이나 공유 저장소(후속)로 바꾼다. 사용자당 동시 연결 한도도 replica 안에서만 센다.
- Gateway 를 재시작하면 열려 있던 연결이 모두 끊기고, 메모리의 `jti` 도 사라진다(남은 ticket 수명은 60초 이하).
- `console_sessions` 는 발급 행이다. 화면이 콘솔을 한 번 열 때 Pod 목록용·연결용으로 두 건이 생긴다. 보존 기간·정리는 정하지 않았다(후속).
- 셸 입출력은 기록하지 않는다. 사용자가 Pod 안에서 한 일은 추적하지 못한다. 필요해지면 별도 결정이다.
- Pod 에 셸이 없는 이미지는 콘솔을 쓸 수 없다(`SHELL_NOT_FOUND`). 디버그 컨테이너(`pods/ephemeralcontainers`)는 이번 범위가 아니다.
- 데이터베이스 서비스(StatefulSet, 컨테이너 이름도 `app`)도 같은 방식으로 붙는다.
- 클러스터 프로토콜은 로컬 K3s v1.33.13(Docker)의 실제 API 서버로 확인했다: Pod 조회, 셸 탐색(bash 이미지·busybox·셸 없는 `pause` 이미지), 대화형 exec(입력·출력·UTF-8·초기 터미널 크기와 resize·종료 코드), 없는 Pod, 잘못된 토큰, 그리고 Control API 가 서명한 ticket → Gateway(uvicorn) → K3s 전체 경로. 그 클러스터에서 ClusterRole 이 `pods/exec` `create` 만 주면 exec 업그레이드가 `403` 이고 `create` + `get` 이면 된다(WebSocket 업그레이드가 GET 이라서). 그래서 `get` 을 함께 준다. 더 새 버전에서 `get` 이 필요 없어지는지는 배포 후 EKS 에서 확인해 불필요하면 뺀다.
- 실제 EKS·ALB 로는 검증하지 못했다. EKS bearer 토큰(`k8s-aws-v1.`)은 AWS 의 `eks get-token` 형식을 따라 만들고 서명 헤더만 단위 테스트로 확인했다. 배포 후 dev 에서 한 번 붙어 확인한다.

## 다음 단계: 온프레미스 (이번 범위 밖)
Control API 는 ONPREM 타깃에 409 `CONSOLE_TARGET_NOT_SUPPORTED` 를 돌려준다. 온프레미스 서버(사용자 K3s, Tailscale 경유)는 다음과 같이 잇는다.

- 서버 설치 스크립트가 서비스 배포용 SA(`iris-argocd`)와 별도로 **exec 전용 SA**(`pods` get·list, `pods/exec` create + get)를 만들고, connect 로 그 토큰을 보낸다(등록 계약 변경). 배포용 SA 는 Secret 까지 읽는 넓은 권한이라 재사용하지 않는다.
- Gateway 는 DB 접속 정보를 갖지 않으므로 그 토큰을 읽는 경로를 정해야 한다. 후보: (a) Deploy Worker 가 서버 토큰을 봉인해 GitOps 로 커밋하고 Argo 가 Gateway namespace 에 Secret 으로 내려준다(기존 권한 모델과 같다. 비밀은 Deploy Worker 가 봉인하고 Argo 가 배포한다). (b) Gateway 가 Control API 의 내부 엔드포인트에서 받는다(서비스 간 인증이 필요하고 Control API 가 자격증명을 다루게 된다). 이 중 (a)를 기본 방향으로 보고, 다음 ADR 에서 확정한다.
- Gateway 는 Argo 가 서버 API 에 닿는 Tailscale egress 와 같은 경로를 쓴다. NetworkPolicy 에서 그 egress 를 연다.
- 코드에서는 ticket 의 `cluster` 클레임(`aws` → 서버 타깃의 클러스터 식별자)과 클러스터 Client 선택 한 곳이 바뀐다.
