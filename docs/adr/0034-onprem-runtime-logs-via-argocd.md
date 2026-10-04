# 0034. on-prem 타깃의 런타임 로그는 Control API 가 Argo CD Pod 로그 API 로 읽는다

- 상태: 제안됨 (iris-infra 의 `iris-log-reader` 토큰 발급·Control API Secret 반영 대기)
- 날짜: 2026-10-04
- 결정자: 김지민

## 배경
서비스 Logs 탭(`GET /services/{id}/logs`·`/logs/stream`), 배포 상세의 Deploy Logs(`deploy-logs`), CLI `likelion logs` 는 모두 Loki 하나에서 읽는다. Loki 에는 AWS workload 클러스터의 수집기가 보낸 로그만 있다. 그래서 on-prem 타깃(공용 `onprem`, 사용자가 등록한 서버 `onprem-{key}`)에 배포한 서비스는 로그가 하나도 보이지 않는다.

Argo CD 는 이미 tailnet 으로 모든 on-prem 클러스터에 붙어 있고(ADR 0029), 클러스터 접속 정보는 읽기 권한을 포함한다. Argo CD 는 Application 이 띄운 Pod 의 로그를 돌려주는 API(`GET /api/v1/applications/{name}/logs`)를 제공한다.

## 검토한 선택지
1. **서버마다 수집기를 깔아 Loki 로 보낸다.** 이력·검색·release 라벨까지 AWS 와 같아진다. 하지만 서버마다 수집기·자격증명·tailnet 경로(Loki 쓰기 엔드포인트 노출)를 새로 만들어야 하고, 설치 스크립트와 iris-infra 를 함께 바꿔야 해 빠르게 낼 수 없다.
2. **Argo CD Pod 로그 API 를 읽는다.** 이미 있는 연결을 쓰므로 인프라는 읽기 전용 토큰 하나만 만들면 된다. 지금 떠 있는 Pod 의 로그만 있고(kubelet 이 가진 만큼) 과거 이력은 없다. Control API 가 Argo CD 를 부르게 되어 "Argo CD 는 Worker 만 부른다"는 경계에 예외가 생긴다.
3. **Control API 가 K3s API 를 직접 읽는다.** Control API 가 서버마다 클러스터 자격증명을 갖게 되고 tailnet 에도 붙어야 한다. "Control API 는 클러스터 접근 금지"가 무너진다.

## 결정
2번. on-prem 타깃의 런타임 로그는 Control API 가 Argo CD Pod 로그 API 로 읽는다. 읽기 전용 예외이고, 쓰기·Sync·exec 는 하지 않는다.

- **분기**: `ObservabilityService` 가 DB 를 읽는 단계(`get_scope`·`get_target_kind`)에서 타깃 종류를 정하고, 외부 조회 단계에서 `ONPREM` 이면 `ArgoCdClient.search_pod_logs` 를, 그 밖에는 Loki 를 읽는다. 응답 모양(`LogEntry`)은 같아 웹·CLI 는 바꾸지 않는다.
- **조회**: Application `svc-{id}`, `namespace=svc-{id}`, `container=app`, `sinceSeconds`(구간 시작부터 지금까지), `tailLines=5000`(Pod 마다), `follow=false`. 구간 끝·검색어(대소문자 구분 부분 문자열)·`limit` 은 받은 뒤 거른다.
- **SSE**: 계약(이벤트 이름·`id`·`Last-Event-ID`·overflow·error·5분 종료)은 그대로 두고, on-prem 은 5초마다 커서부터 다시 읽는다. `follow=true` 는 연결을 오래 쥐고 끊김 처리가 복잡해 쓰지 않는다.
- **오류**: Application 이 없으면(403·404) 빈 목록, 그 밖의 HTTP 오류·스트림 안의 `{"error": ...}`·형식 오류는 `ExternalError`(502)다. 토큰·주소가 없으면 `NotConfiguredError`(503)다.
- **메트릭·네트워크 로그**: on-prem 은 수집하지 않으므로 `metrics`·`traffic-metrics`·`network-logs` 는 가짜 값 대신 `503 NOT_CONFIGURED` 를 준다.
- **설정**: Control API 전용 `ARGOCD_SERVER_URL`·`ARGOCD_LOGS_TOKEN`. Deploy Worker 의 `ARGOCD_TOKEN` 과 공유하지 않는다(컴포넌트끼리 자격증명을 공유하지 않는다).

### 토큰 (iris-infra 가 발급)
Argo project `iris-svc-project` 의 role `iris-log-reader`, 정책은 다음 두 줄뿐이다.

```text
p, proj:iris-svc-project:iris-log-reader, applications, get, iris-svc-project/*, allow
p, proj:iris-svc-project:iris-log-reader, logs, get, iris-svc-project/*, allow
```

CLI 로 만들 때:

```bash
argocd proj role create iris-svc-project iris-log-reader \
  --description "Control API on-prem runtime logs (read-only)"
argocd proj role add-policy iris-svc-project iris-log-reader \
  --resource applications --action get --object '*' --permission allow
argocd proj role add-policy iris-svc-project iris-log-reader \
  --resource logs --action get --object '*' --permission allow
argocd proj role create-token iris-svc-project iris-log-reader   # 출력값을 ARGOCD_LOGS_TOKEN 으로
```

AppProject 를 선언으로 관리하면 `spec.roles` 에 같은 role·policies 를 넣고 토큰만 `create-token` 으로 만든다. Control API Pod 에서 `argocd-server` 로 가는 네트워크(NetworkPolicy)가 열려 있어야 한다.

## 결과
- on-prem 서비스도 Logs 탭·SSE·Deploy Logs·`likelion logs`·AI 진단의 런타임 로그가 나온다.
- **한계**: 지금 떠 있는 Pod 의 로그만 있다. 재시작·교체로 사라진 Pod, 컨테이너 로그 회전으로 지워진 줄은 없다. Deploy Logs 는 release 구분 없이 구간으로만 거르므로 교체된 배포의 로그는 비어 있을 수 있다. 검색은 받은 줄(Pod 마다 최대 5000줄) 안에서만 한다. Argo CD 의 `max pods to view logs`(기본 10) 를 넘는 Pod 수면 오류(502)다.
- 매 조회가 Argo CD → tailnet → kubelet 을 거쳐 Loki 보다 느리다. SSE 는 연결마다 5초 간격으로 Argo CD 를 부른다.
- Control API 가 Argo CD 를 부르는 유일한 경로다. 로그 외 용도로 넓히지 않는다. 수집기를 서버에 깔게 되면(선택지 1) 이 경로를 걷어 낸다.
