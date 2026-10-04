# 0035. on-prem 타깃의 서비스 콘솔은 Console Gateway 가 Argo CD 터미널을 중계한다

- 상태: 제안됨 (iris-infra 의 Argo `exec.enabled`·`iris-console` role 토큰·서버 ClusterRole 적용과 운영 E2E 대기)
- 날짜: 2026-10-04
- 결정자: 김지민
- 보강: [ADR 0033](0033-service-console-via-console-gateway.md) 의 "다음 단계: 온프레미스" 를 이 결정으로 대신한다(그 절의 exec 전용 SA 안은 채택하지 않는다).

## 배경
서비스 콘솔(실행 중인 Pod 의 `app` 컨테이너 셸)은 AWS 타깃에서만 열린다([ADR 0033](0033-service-console-via-console-gateway.md)). ONPREM 타깃(공용 `onprem`, 사용자가 등록한 서버 `onprem-{key}`)은 Control API 가 `409 CONSOLE_TARGET_NOT_SUPPORTED` 를 돌려준다.

그 사이 사정이 바뀌었다.
- Argo CD(management)는 이미 tailnet 으로 모든 on-prem 클러스터에 붙어 있고, 서버별 ServiceAccount 자격증명을 갖고 있다([ADR 0029](0029-user-registered-onprem-servers.md)).
- Control API 가 Argo CD 를 읽기 전용으로 부르는 선례가 생겼다([ADR 0034](0034-onprem-runtime-logs-via-argocd.md)). 프로젝트 role 토큰, `argocd-server` NetworkPolicy, CA bundle 마운트가 이미 정리돼 있다.
- Argo CD 3.x(chart 10.9.6 = **Argo CD v3.5.3**, iris-infra `helm/versions.json`)에는 Application 의 Pod 에 셸을 여는 내장 터미널(`GET /terminal`, WebSocket)이 있다. iris-infra 의 `helm/bootstrap/values.yaml` 에서는 `"exec.enabled": "false"` 로 꺼 둔 상태다.

서버의 설치 스크립트가 만든 배포용 SA(`iris-argocd`)에는 `pods/exec` 가 없다. 서버마다 새 토큰을 만들어 저장하고 Gateway 가 읽게 하는 경로는 등록 계약·설치 스크립트·CLI·저장소가 모두 바뀌는 큰 일이다.

## 검토한 선택지
1. **Gateway 가 Argo CD 터미널(`/terminal`)을 중계한다.** 새 자격증명 저장이 없다. Argo 가 서버 클러스터 접속(tailnet·SA 토큰)을 이미 갖고, "그 Application 의 Pod 인가" 검사를 서버에서 한다. 서버 SA 의 ClusterRole 에 `pods/exec` 를 더하고, Argo 의 `exec.enabled` 와 프로젝트 role 하나가 필요하다. 터미널 프로토콜은 Argo UI 가 쓰는 내부 인터페이스라 Argo 버전에 묶인다.
2. **서버마다 exec 전용 SA 를 만든다**(ADR 0033 "다음 단계"의 원안). 설치 스크립트가 `pods`(get·list)·`pods/exec` 만 가진 SA 를 더 만들고 connect 로 토큰을 보내, Control API 가 암호화해 저장한다. Gateway 가 그 토큰을 읽는 경로(Deploy Worker 가 봉인해 GitOps 로 내리거나 Control API 내부 엔드포인트)와 Tailscale egress 접근을 더해야 한다. 최소 권한이지만 등록 계약·설치 스크립트·`onprem_servers` 스키마·CLI·Gateway 가 모두 바뀌고, 이미 연결된 서버는 토큰을 다시 보낼 방법이 없다(아래 "이미 등록된 서버").
3. **Control API 가 K3s API 를 직접 부른다.** Control API 가 서버마다 클러스터 자격증명을 갖고 tailnet 에 붙어야 한다. "Control API 는 클러스터 접근 금지"가 무너지므로 채택하지 않는다.

## 결정
1번. AWS 타깃은 ADR 0033 의 방식(Gateway → Prod EKS API 직접 exec)을 그대로 둔다. ONPREM 타깃만 Gateway 가 Argo CD 터미널로 이어 준다. 브라우저와 Gateway 사이 프로토콜(`docs/console-api.md`)은 바뀌지 않는다.

### Argo CD v3.5.3 터미널 (소스로 확인한 사실)
`server/application/terminal.go`·`websocket.go`·`util/session/sessionmanager.go`·`server/server.go` 를 v3.5.3 태그에서 읽어 확인했다.
- **경로·쿼리**: `GET /terminal?pod=&container=&appName=&appNamespace=&projectName=&namespace=`(+선택 `shell`). `pod`·`container`·`appName`·`projectName`·`namespace` 는 필수다. `shell` 은 허용 목록(`exec.shells`, 기본 `bash,sh,powershell,cmd`)에 있을 때만 쓰고, 없으면 목록을 차례로 시도한다.
- **인증**: `/terminal` 은 `WithAuthMiddleware` 로 감싸이고, 이 미들웨어와 `getToken` 은 JWT 를 **쿠키 `argocd.token` 에서만** 읽는다(`Authorization` 헤더는 보지 않는다). 프로젝트 role 토큰도 이 쿠키로 넣으면 된다. 그래서 Gateway 는 WebSocket 핸드셰이크에 `Cookie: argocd.token=<토큰>` 을 붙인다. Pod 목록(REST `resource-tree`)은 기존대로 `Authorization: Bearer` 다.
- **권한**: `applications, get` 과 `exec, create` 를 `<project>/<app>` 대상으로 검사하고 둘 중 하나라도 없으면 **401** 이다(403 이 아니다). 정책 문자열은 `p, proj:iris-svc-project:iris-console, applications, get, iris-svc-project/*, allow` 와 `p, proj:iris-svc-project:iris-console, exec, create, iris-svc-project/*, allow`.
- **범위 검사**: 핸드셰이크 전에 Application 이 그 project 에 속하는지, Pod 가 **Application 의 리소스 트리에 있는지**(`Pod doesn't belong to specified app` → 400), Pod 가 존재하는지(`Cannot find pod` → 400), 컨테이너가 실행 중인지(`container find running` → 400)를 본다. Application 이 없으면 404(본문 `App not found`).
- **기능 꺼짐**: `exec.enabled` 가 꺼져 있으면 본문 없이 **404** 다(Application 없음과 본문으로 구분한다).
- **메시지**(JSON 텍스트 프레임, `TerminalMessage{operation, data, rows, cols}`): 클라이언트→서버 `{"operation":"stdin","data":"…"}`·`{"operation":"resize","cols":N,"rows":N}`, 서버→클라이언트 `{"operation":"stdout","data":"…"}`(stdout 과 stderr 가 합쳐진 TTY 출력). 토큰 갱신 때 `{"Code":1}` 같은 제어 프레임이 올 수 있어 `operation` 이 없는 프레임은 무시한다. 서버는 5초마다 WebSocket ping 을 보내고 `CheckOrigin` 은 항상 true 다.
- **종료**: 셸이 끝나거나 모든 셸 시도가 실패하면 서버가 연결을 닫는다(종료 코드를 알려 주지 않고, 업그레이드 뒤라 HTTP 오류 본문도 갈 수 없다). 그래서 on-prem 의 `exit` 프레임은 `code` 가 없다.
- **exec 방식**: SPDY(POST, `create pods/exec`)를 먼저 시도하고 WebSocket(GET, `get pods/exec`)을 폴백으로 쓴다. 서버 SA 의 ClusterRole 에는 `get` 과 `create` 를 모두 준다.
- **Pod 목록**: `GET /api/v1/applications/svc-{id}/resource-tree?appNamespace=argocd`. 노드 중 `kind: Pod`·`group` 없음·namespace `svc-{id}` 가 대상이고, `info` 의 `Status Reason`(Running·Pending·CrashLoopBackOff·Terminating 등)이 단계, `health.status == Healthy` 와 `Status Reason == Running` 이 준비 여부, `createdAt` 이 시작 시각이다. 노드에는 Pod 라벨이 없어(`resource.customLabels` 를 설정하지 않는 한) `releaseId` 는 모른다. 이 값은 응답에서 생략한다(계약 §4: 모르면 생략).

### 변경
- **Control API**: `ConsoleService` 가 ONPREM 타깃을 받는다. 판정 순서는 타깃 종류 → 설정 → 서버 연결 → release 다. 사용자가 등록한 서버(타깃 `owner_id` 있음)는 `onprem_servers` 연결 상태가 `CONNECTED` 여야 하고(하트비트가 끊긴 `DISCONNECTED` 는 `CONNECTED` 가 아니다), 아니면 `GET …/console` 은 `reason=TARGET_NOT_CONNECTED`, `POST …/console/sessions` 는 `409 TARGET_NOT_CONNECTED`(배포 요청의 같은 이름 오류를 재사용)다. 공용 `onprem` 타깃은 서버 행이 없으므로 release 조건만 본다. ticket 의 `cluster` 는 ONPREM 이면 `onprem`, AWS 면 `aws` 다.
- **Gateway**: ticket 의 `cluster` 로 Client 를 고른다(`aws` → `HttpKubernetesClient`, `onprem` → `ArgoCdTerminalClient`). 두 Client 는 같은 Protocol 이고 Application 이름은 namespace 와 같은 `svc-{id}` 다. 요청에서 namespace·컨테이너·Application 을 받지 않는다(ticket 에서 계산한다). 설정(`CONSOLE_ARGOCD_SERVER_URL`·`CONSOLE_ARGOCD_TOKEN`)이 없으면 onprem ticket 은 `CLUSTER_UNAVAILABLE` 이고, AWS 설정만 있어도 그 반대여도 Gateway 는 뜬다(둘 다 없으면 시작하지 않는다). Gateway 는 여전히 DB 접속 정보가 없다.
- **셸**: Argo 가 셸을 고르므로 `ready.shell` 은 on-prem 에서 생략한다(웹은 이미 선택 필드로 받는다). Gateway 는 연결한 뒤 첫 출력(프롬프트)을 최대 5초 기다린다. 첫 출력 전에 연결이 끊기면 `SHELL_NOT_FOUND` 로 알린다. 셸이 없는 이미지와 서버 SA 에 `pods/exec` 가 없는 경우(Argo 가 모든 셸 시도에서 실패)를 Argo 가 구분해 주지 않아 같은 코드가 된다. 로그에는 `argo_closed_before_output` 로 남겨 운영자가 구분한다.
- **오류 매핑**(핸드셰이크): 400 `Pod doesn't belong…`·`Cannot find pod` → `POD_NOT_FOUND`, 400 `container find running` → `POD_NOT_READY`, 404 `App not found` → `POD_NOT_FOUND`, 그 밖의 400·401·404(본문 없음 = `exec.enabled` 꺼짐)·5xx·연결 실패 → `CLUSTER_UNAVAILABLE`.
- **서버 SA 권한**: 설치 스크립트(`app/assets/onprem/install.sh`)의 ClusterRole `iris-onprem-service-deployer` 에 `{apiGroups: [""], resources: [pods/exec], verbs: [get, create]}` 를 더한다. iris-infra `clusters/onprem-workload/argocd-service-deployer.yaml`(공용 `onprem` 서버)도 같아야 한다.

### 이미 등록된 서버
설치 스크립트를 다시 실행하면 ClusterRole 이 `kubectl apply` 로 제자리 갱신돼 스크립트 자체는 멱등이다. 하지만 `CONNECTED` 서버는 `bootstrap` 이 등록 토큰을 거절하고(1회용), `CONNECTED` 에서는 토큰을 재발급할 수도 없어 **다시 실행할 수 없다**. 그래서 스크립트에 토큰 없이 RBAC 만 갱신하는 `--rbac-only` 모드를 더한다(root 로 `curl … | sudo bash -s -- --rbac-only`, K3s 가 이미 있어야 한다). 반영 전의 서버로는 콘솔이 `SHELL_NOT_FOUND` 로 보인다(위 한계). 웹·CLI 가 서버별 RBAC 반영 여부를 알 방법은 없다(후속).

## 결과
- 사용자는 on-prem 서비스에도 같은 화면에서 셸을 연다. 새 자격증명 저장·등록 계약 변경·DB 변경이 없다. Control API 는 여전히 클러스터와 Argo CD 를 부르지 않는다(ticket 서명뿐). Gateway 만 Argo CD 를 부른다.
- **Argo CD 터미널이 켜진다.** `exec.enabled` 는 Argo 전체 설정이라, `exec` 권한이 있는 모든 Argo 계정(admin 포함)이 터미널을 쓸 수 있게 된다. 지금 Argo 접근은 플랫폼 관리자만이다. 이 결정은 Argo 의 `exec` RBAC 을 `iris-console` role 외에 새로 주지 않는 것을 전제로 한다.
- **`iris-console` role 은 project `iris-svc-project` 의 모든 Application 에 exec 를 허용한다.** 어느 Application·Pod 에 붙는지는 Gateway 가 ticket(`svc`)에서 계산해 고정하므로, Gateway 가 침해되면 그 범위가 곧 피해 범위다(AWS 서비스 Application 도 같은 project 이지만 AWS workload 의 Argo SA 에는 `pods/exec` 가 없어 거절된다). 토큰은 만료를 두고 주기적으로 교체한다(iris-infra runbook).
- 서버 SA 의 ClusterRole 이 사용자 서버의 모든 Pod 에 대한 exec 를 허용한다. SA 가 이미 Secret 읽기와 워크로드 생성 권한을 갖고 있어 사용자 서버에서 플랫폼이 할 수 있는 일이 크게 늘지는 않지만, 코드 실행 권한이 더해지는 것은 사실이다. 1차 방어는 Argo 의 Application 범위 검사이고 컨테이너는 Gateway 가 `app` 으로 고정한다. AWS 타깃에 있는 admission policy 같은 2차 방어는 사용자 서버에 없다.
- 터미널 프로토콜은 Argo UI 의 내부 인터페이스다. Argo 를 올릴 때(chart 갱신) 프레임 형식·인증이 바뀌지 않았는지 이 문서의 사실과 대조해야 한다.
- 셸 입출력은 기록하지 않는다(ADR 0033 과 같다). Argo 서버 로그에는 접속 사실(`terminal session starting`, Application·Pod·사용자 이름)이 남는다.
- 종료 코드를 알 수 없고 `SHELL_NOT_FOUND` 의 원인이 모호하다(위).
- 실제 Argo 터미널 연결은 운영에서 `exec.enabled` 를 켜기 전에는 시험할 수 없었다. 단위·종단 테스트는 v3.5.3 소스에서 확인한 프로토콜을 따르는 가짜 Argo 서버로 했다. 운영 검증 항목은 PR 본문과 iris-infra runbook 에 있다: 쿠키 인증, 프로젝트 role 토큰의 `exec` 권한, `resource-tree` 의 Pod 노드 모양, 셸 없는 이미지·RBAC 없는 서버에서의 종료 동작, ALB 를 거친 연결 유지.
