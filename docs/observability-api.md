# 서비스 로그·메트릭 API

런타임 컨테이너 로그와 리소스 메트릭 조회다. CodeBuild 빌드 로그, HTTP 요청 로그 전용 파싱, 요청 수·오류율·응답 시간은 이 API의 범위에 포함하지 않는다. 배포 상세 화면의 빌드·배포·네트워크 로그는 배포 단위 API 로 따로 있다([배포 상세 화면 API](deployment-details-api.md)).

## 운영 연결

수집·저장은 iris-infra가 맡는다(`docs/runbooks/observability.md`). workload 클러스터의 OTel agent가 `svc-*` Pod 로그와 kubelet 메트릭을 management 클러스터로 보내고, Loki와 Prometheus가 **Control API와 같은 management 클러스터**에 저장한다. 클러스터 간 조회 경로는 필요 없다.

Control API에 두 주소를 환경변수로 주입한다(Secret `iris-platform-was-env`). AWS target 은 모두 같은 백엔드를 쓴다(수집 대상이 AWS workload 클러스터 하나다). 값이 없으면 `503 NOT_CONFIGURED`다.

**on-prem target**(공용 `onprem`·사용자 등록 서버 `onprem-{key}`)은 수집 대상이 아니다. 런타임 로그(`logs`·`logs/stream`·배포의 `deploy-logs`)만 Control API 가 Argo CD Pod 로그 API(`GET /api/v1/applications/svc-{id}/logs`, container `app`, Pod 마다 최대 5000줄)로 읽어 같은 모양으로 돌려준다. 지금 떠 있는 Pod 의 로그만 있고(과거 이력·release 구분 없음), 검색은 받은 줄 안에서 하며, SSE 는 5초마다 다시 읽는다. 메트릭·트래픽 지표·네트워크 로그는 `503 NOT_CONFIGURED` 다. 설정은 `ARGOCD_SERVER_URL`·`ARGOCD_LOGS_TOKEN`(없으면 on-prem 로그만 `503 NOT_CONFIGURED`), 설계는 [ADR 0034](adr/0034-onprem-runtime-logs-via-argocd.md).

```dotenv
LOKI_URL=http://loki.observability:3100
PROMETHEUS_URL=http://monitoring-prometheus.observability:9090
```

- Loki 라벨: `k8s_namespace_name`(`svc-{serviceId}`), `k8s_pod_name`, `k8s_container_name`, `iris_release_id`. API는 container `app`만 조회한다.
- Prometheus 메트릭(kubeletstats, Pod 단위): `k8s_pod_cpu_time_seconds_total`, `k8s_pod_memory_working_set_bytes`, `k8s_pod_network_io_bytes_total{direction="receive"|"transmit"}`. 라벨 `k8s_namespace_name`으로 서비스를 고른다.
- 두 백엔드는 management 내부 ClusterIP이며 인증이 없다. iris-platform chart의 NetworkPolicy가 API Pod만 3100·9090으로 나가게 연다.
- AWS workload 클러스터는 하나라 namespace만으로 서비스를 구분한다. 클러스터가 늘면 target별 주소로 나누고, 수집기에 `k8s.cluster.name`을 넣어 selector에 더한다.
- 설정 누락은 `503 NOT_CONFIGURED`, 백엔드 오류/타임아웃/잘못된 응답은 `502 EXTERNAL_ERROR`다. 데이터가 없으면 빈 배열을 반환한다.
- API가 서비스 소유권과 연결된 target을 확인한다. 임의 LogQL/PromQL, namespace, 백엔드 주소는 클라이언트에서 받지 않는다. GitHub App 설정 없이도 이 API를 사용할 수 있다(로그인 세션 설정은 필요).

## 과거 로그

`GET /api/v1/services/{serviceId}/logs?targetId=1&start=2026-10-01T00:00:00Z&end=2026-10-01T01:00:00Z&limit=200&search=error`

`start`, `end`는 타임존이 있는 ISO 8601 시각이다. 최대 7일, 미래 시각은 받지 않는다. `limit`은 1~1000(기본 200). `search`는 대소문자를 구분하는 부분 문자열이다.

```json
{"success":true,"data":{"entries":[{"timestampNs":"1790812800000000000","message":"ready","pod":"app-abc-xyz","container":"app"}],"isTruncated":false}}
```

최근 `limit`개를 가져와 시간 오름차순으로 반환한다. `isTruncated=true`는 제한에 도달했음을 뜻한다(실제 전체 개수가 제한과 같은 경우도 포함). 과거 데이터를 더 조회하려면 시간 범위를 좁힌다. timestampNs는 JS 정밀도 손실을 막기 위해 문자열이다.

## 실시간 로그 SSE

`GET /api/v1/services/{serviceId}/logs/stream?targetId=1&search=error&cursor=1790812800000000000`

쿠키 또는 Bearer 인증을 사용한다. 재연결 시 `Last-Event-ID`가 `cursor`보다 우선한다. 커서는 지난 7일 이내 ns timestamp다. 최초 연결은 최근 10초부터 시작한다.

```text
id: 1790812800000000001
event: logs
data: [{"timestampNs":"1790812800000000000","message":"ready","pod":"app-abc-xyz","container":"app"}]

```

- `logs`는 로그 배열이며 `id`는 다음 시작 커서다. 데이터 내부 줄바꿈은 JSON으로 escape한다.
- 2초 간격으로 Loki를 조회하고 데이터가 없으면 SSE comment heartbeat를 보낸다. 현재 연결에서는 10초 겹침 구간을 조회하여 늦게 수집된 로그를 포함하고 동일 항목을 중복 제거한다.
- 5분 후 연결을 종료한다. EventSource가 재연결하면서 인증·서비스 접근 권한을 다시 확인한다. 화면을 떠날 때 `close()`한다.
- 조회 한 번에 1000개에 도달하면 `overflow` 이벤트(`LOG_STREAM_OVERFLOW`) 후 종료한다. 소비자는 연결을 닫고 시간 범위를 좁혀 과거 로그를 조회한다. 커서를 건너뛰며 유실을 숨기지 않는다.
- 연결 후 백엔드 장애는 `error` 이벤트(`EXTERNAL_ERROR`) 후 종료한다. 최초 조회 장애는 SSE 응답 시작 전 일반 JSON 502다.
- 이 스트림은 영구적인 전달 큐가 아니다. 수집 지연이 10초를 넘거나 재연결 중 이전 timestamp로 늦게 들어온 로그는 과거 조회로 확인한다. 클라이언트도 timestampNs·pod·container·message로 겹침 항목을 중복 제거할 수 있다.
- 프록시 버퍼링을 끄고 ALB idle timeout은 heartbeat 간격보다 길게 둔다. API는 `Cache-Control: no-cache`, `X-Accel-Buffering: no`를 보낸다.

```javascript
const source = new EventSource('/api/v1/services/42/logs/stream?targetId=1', {
  withCredentials: true,
});
source.addEventListener('logs', (event) => {
  const entries = JSON.parse(event.data);
  // entries를 로그 화면에 추가한다. 화면의 최대 보관 개수도 제한한다.
});
source.addEventListener('overflow', () => source.close());
// 네트워크 error는 EventSource가 재연결한다. 서버 error 이벤트는 data에 code가 있다.
source.addEventListener('error', (event) => {
  if (event instanceof MessageEvent && event.data) source.close();
});
// 화면 unmount 시 source.close()
```

## 메트릭

`GET /api/v1/services/{serviceId}/metrics?targetId=1&start=2026-10-01T00:00:00Z&end=2026-10-01T01:00:00Z&step=60`

`step`은 초 단위 15~86400(기본 60). 범위/step이 1440을 초과하면 422를 반환한다. 7일 범위에는 최소 420초 step을 사용한다.

```json
{"success":true,"data":[{"metric":"cpu","unit":"cores","points":[{"timestamp":1790812800,"value":0.25}]},{"metric":"memory","unit":"bytes","points":[]},{"metric":"network_receive","unit":"bytes/s","points":[]},{"metric":"network_transmit","unit":"bytes/s","points":[]}]}
```

`groupBy`는 `total`(기본) 또는 `pod`이다. `total`은 서비스 전체 합계로 metric당 시리즈 1개를 주고 위 응답과 같다. `pod`은 Pod(replica)별로 나눠 metric당 Pod 수만큼 시리즈를 주며, 각 항목에 `pod` 이름이 붙는다. 화면의 Sum/Replicas 토글에 대응한다.

`GET /api/v1/services/{serviceId}/metrics?targetId=1&start=...&end=...&step=60&groupBy=pod`

```json
{"success":true,"data":[{"metric":"cpu","unit":"cores","pod":"app-5d9c7b8f6d-x2k4q","points":[{"timestamp":1790812800,"value":0.12}]},{"metric":"cpu","unit":"cores","pod":"app-5d9c7b8f6d-z9m7w","points":[{"timestamp":1790812800,"value":0.13}]}]}
```

- 시리즈는 metric 순서(`cpu`, `memory`, `network_receive`, `network_transmit`) 안에서 `pod` 이름순이다. Pod마다 살아 있던 구간의 포인트만 있어서 시리즈마다 길이가 다를 수 있다.
- 데이터가 없으면 `total`은 빈 `points`를 가진 항목을 주지만 `pod`은 그 metric의 항목을 주지 않는다(전부 없으면 빈 배열).
- 롤링 배포가 잦은 긴 범위는 Pod 이름이 계속 바뀐다. 범위 안의 Pod이 metric당 50개를 넘으면 잘라내지 않고 `422`로 거절한다. 시간 범위를 좁히거나 `total`을 쓴다.
- Pod 라벨은 Loki와 같은 OTel 리소스 속성에서 온 `k8s_pod_name`이다. 라벨 이름은 배포 후 실제 `groupBy=pod` 응답으로 확인한다.

CPU는 Pod CPU 시간의 5분 rate를 core 단위로, 메모리는 Pod working set을 byte 단위로 집계한다. 네트워크는 Pod 인터페이스의 5분 rate이며 공용 인터넷 트래픽만 분리한 값이 아니다. 수집 주기가 30초라 최근 1분 안팎은 비어 있을 수 있다. NaN/Inf 샘플은 제외하고, 없는 메트릭을 0으로 채우지 않는다. 프론트는 timestamp를 실제 시간축에 매핑하고 30초 정도 간격으로 조회한다.

## 트래픽 지표

요청 수·오류율·응답 시간·공용 네트워크다. 메트릭 API(Pod CPU·메모리·네트워크)와 별개 엔드포인트다. 의미(15분 지연, 결측, 응답 시간 집계 방식)가 다르기 때문이다.

`GET /api/v1/services/{serviceId}/traffic-metrics?targetId=1&start=2026-10-01T00:00:00Z&end=2026-10-01T01:00:00Z&step=60`

출처는 iris-infra 가 ALB 접근 로그를 Loki 로 정규화하고 Loki ruler 가 1분마다 Prometheus 로 remote write 하는 gauge 다(iris-infra `contracts/service-traffic.md`). 서비스는 라벨 `cluster`(`TRAFFIC_CLUSTER`, 기본 `iris-dev-workload`)와 `k8s_namespace_name`(`svc-{id}`)으로 고르므로 Pod 수·TargetGroup 수와 무관하다. 백엔드 주소는 `PROMETHEUS_URL` 이고 없으면 `503 NOT_CONFIGURED`다.

`start`, `end`는 요청이 일어난 **이벤트 시각**이다. `step`은 60~86400초이고 범위÷step 이 1440 을 넘으면 422 다. 7일 범위에는 최소 420초 step 을 쓴다.

```json
{"success":true,"data":{"availableUntil":1790951100.0,"series":[{"metric":"requests","unit":"requests","points":[{"timestamp":1790950200,"value":12}]},{"metric":"error_rate_5xx","unit":"ratio","points":[{"timestamp":1790950200,"value":0.08}]}]}}
```

| metric | unit | 의미 |
|---|---|---|
| `requests` | requests | 버킷 안에 ALB 가 완료한 요청 수 합계 |
| `error_rate_4xx` | ratio | 같은 버킷의 4xx 응답 수 ÷ 요청 수 (0~1, % 는 ×100) |
| `error_rate_5xx` | ratio | 같은 버킷의 5xx 응답 수 ÷ 요청 수 |
| `public_network_receive` | bytes/s | ALB `received_bytes` 의 버킷 평균 |
| `public_network_transmit` | bytes/s | ALB `sent_bytes` 의 버킷 평균 |
| `response_time_avg` | seconds | 버킷에서 마지막으로 집계된 5분 구간의 `target_processing_time` 평균 |
| `response_time_p50`, `response_time_p95` | seconds | 같은 5분 구간의 p50, p95 |

- 시리즈 8개가 항상 이 순서로 온다. 데이터가 없는 지표는 `points` 가 빈 배열이다.
- `timestamp`는 버킷이 끝나는 이벤트 시각이다. 버킷은 서로 겹치지 않는다.
- **집계 지연**: ruler 가 15분 늦게 평가해서 최신 약 15분은 아직 집계되지 않았다. `availableUntil`(Unix 초, 현재−15분) 이후는 "비어 있음"이 아니라 "아직 모름"이다. "Last 15 min" 같은 최근 범위는 거의 비어 온다. 15분은 보장된 최대 지연이 아니라서 수집이 더 늦으면 과거 샘플은 자동으로 고쳐지지 않는다.
- **결측은 0 이 아니다**: 요청이 없는 분에는 샘플이 만들어지지 않아 점이 없다. 접속이 없는 구간과 수집기 장애를 이 API 만으로는 구분하지 못한다. 점이 없으면 그대로 비워 그린다.
- 오류는 사용자에게 돌려준 ALB 최종 상태 코드이고, TargetGroup 이 없는 redirect·default action·차단 응답은 서비스에 귀속하지 않는다.
- 응답 시간은 ALB 가 target 에 요청을 보낸 뒤 응답 헤더를 받기까지다. 브라우저 체감 시간이나 다운로드 시간이 아니다. 5분 구간 지표라 더 긴 구간으로 평균내거나 합치지 않고, 버킷마다 마지막 5분 구간 값을 그대로 준다(step 이 5분보다 길면 구간 사이가 건너뛰어진다).
- 공용 바이트는 이 외부 ALB 를 통과한 요청·응답 바이트다. Pod 내부 네트워크, 앱이 외부로 호출한 egress, TCP/TLS 오버헤드는 포함하지 않는다. 메트릭 API 의 `network_receive`·`network_transmit` 은 Pod 네트워크라 다른 값이다.
- ALB 접근 로그는 best effort 라 청구·정산 용도가 아니다. `local` target 은 ALB 를 쓰지 않아 빈 결과가 나온다.
- 에러: Prometheus 주소 없음 `503 NOT_CONFIGURED`, 백엔드 오류·잘못된 응답·시리즈 2개 이상 `502 EXTERNAL_ERROR`.

## 검증 범위

MockTransport 및 API 테스트로 쿼리 제한, 소유권, 입력 검증, 응답 변환, SSE 커서·heartbeat·오류·종료를 검증한다. 라벨·메트릭 이름은 2026-10-02 dev 클러스터의 Loki·Prometheus에서 확인했다. API를 통한 운영 조회는 `LOKI_URL`·`PROMETHEUS_URL` 설정과 배포 후 확인한다. DB 스키마 변경은 없다.

트래픽 지표는 계약 문서(iris-infra `contracts/service-traffic.md`)대로 쿼리를 만들고 MockTransport 로 쿼리 문자열, 시각 이동(15분), 결측·NaN 처리, 시리즈 수 검증을 확인했다. 생성한 PromQL 은 문법 파서로 검사했다. **실제 Prometheus 에서의 값과 ruler 가 올리는 라벨은 확인하지 못했다.** iris-infra 의 dev 배포와 서비스 트래픽 검증 뒤에 이 API 로 조회해 확인한다.

외부 API 계약: [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/), [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/).
