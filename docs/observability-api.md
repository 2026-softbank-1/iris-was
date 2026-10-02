# 서비스 로그·메트릭 API

런타임 컨테이너 로그와 리소스 메트릭 조회다. CodeBuild 빌드 로그, HTTP 요청 로그 전용 파싱, 요청 수·오류율·응답 시간은 이 API의 범위에 포함하지 않는다.

## 운영 연결

Control API에 `OBSERVABILITY_ENDPOINTS`를 JSON으로 주입한다. 키는 DB의 target ID이며, 각 주소는 **해당 타깃 클러스터만 조회하는** 내부 Loki/Prometheus 주소여야 한다. 여러 클러스터의 같은 `svc-{id}` 네임스페이스를 함께 저장하는 전역 백엔드는 별도의 클러스터 선택자가 필요하므로 현재 연결 계약에 맞지 않는다.

```dotenv
OBSERVABILITY_ENDPOINTS='{"1":{"loki_url":"http://loki.observability.svc:3100","prometheus_url":"http://monitoring-kube-prometheus-prometheus.observability.svc:9090"}}'
```

예시 주소를 그대로 적용하지 말고 실제 Service 주소와 DB target ID로 바꾼다. 관리 클러스터의 Control API에서 workload 클러스터의 백엔드에 도달하는 내부 네트워크 경로가 필요하다. workload의 `.svc` 이름은 관리 클러스터에서 자동으로 해석되지 않는다.

- Loki 로그 수집기는 workload의 컨테이너 stdout/stderr를 수집하고 `namespace`, `pod`, `container` 라벨을 붙여야 한다. 현재 `iris-infra`의 Prometheus 구성만으로 로그가 수집되지는 않는다. Loki와 수집기는 별도 설치가 필요하다.
- Prometheus는 kubelet/cAdvisor를 scrape해야 한다. 이 API는 `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `container_network_receive_bytes_total`, `container_network_transmit_bytes_total`을 조회한다.
- 배포 chart 계약은 namespace `svc-{serviceId}`, Deployment `app`, container `app`이다. 네트워크는 `app-*` Pod 전체를 집계한다.
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

CPU는 5분 rate를 core 단위로, 메모리는 working set을 byte 단위로 집계한다. 네트워크는 Pod 인터페이스의 5분 rate이며 공용 인터넷 트래픽만 분리한 값이 아니다. NaN/Inf 샘플은 제외하고, 없는 메트릭을 0으로 채우지 않는다. 프론트는 timestamp를 실제 시간축에 매핑하고 30초 정도 간격으로 조회한다.

## 검증 범위

MockTransport 및 API 테스트로 쿼리 제한, 소유권, 입력 검증, 응답 변환, SSE 커서·heartbeat·오류·종료를 검증한다. 실제 Loki/수집기 설치, 클러스터 간 연결, 운영 로그·메트릭 조회 성공은 운영 설정 후 별도로 확인해야 한다. DB 스키마 변경은 없다.

외부 API 계약: [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/), [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/).
