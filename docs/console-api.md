# 서비스 콘솔 API (Pod 셸)

서비스 화면의 콘솔 탭에서 실행 중인 레플리카(Pod)의 `app` 컨테이너에 셸을 여는 계약이다. 설계 배경과 권한 경계는 [ADR 0033](adr/0033-service-console-via-console-gateway.md)(AWS)·[ADR 0035](adr/0035-onprem-console-via-argocd-terminal.md)(온프레미스).

- 대상은 **AWS 타깃**(Prod EKS)과 **온프레미스 타깃**(공용 `onprem`, 사용자가 등록한 서버)이다. 화면과 Gateway 사이 프로토콜은 같고, Gateway 가 AWS 는 클러스터 API 로, 온프레미스는 Argo CD 터미널로 Pod 에 닿는다. 화면이 구분해야 할 것은 아래 "온프레미스" 절의 차이뿐이다.
- **Control API 는 클러스터에 접근하지 않는다.** 소유권을 확인해 60초짜리 ticket 을 발급하기만 하고, 셸 연결은 별도 컴포넌트 **Console Gateway** 가 중계한다.
- Control API 응답은 `ApiResponse` 봉투·camelCase 다. Gateway 의 REST 도 같은 봉투를 쓴다. WebSocket 프레임은 아래 JSON 이다.

```text
화면                         Control API                    Console Gateway                 Prod 클러스터
 │ GET  …/console?targetId      │ 소유·타깃·떠 있는 release 확인
 │◀── available / reason ───────│
 │ POST …/console/sessions      │ ticket 서명(60초) + 발급 기록
 │◀── token, gateway.* ─────────│
 │ GET  {httpUrl}/v1/pods  (Bearer token) ───────────────────▶│ Pod 목록(app 컨테이너)
 │ POST …/console/sessions  (새 ticket)                          │
 │ WS   {wsUrl}/v1/exec?pod=…                                   │
 │ {"type":"auth","token":…} ────────────────────────────────▶│ ticket 검증(1회용) ────────▶ pods/exec
 │◀── {"type":"ready"} · output … ────────────────────────────│◀── 셸 입출력 ───────────────│
```

## 1. Control API

인증은 쿠키 또는 `Authorization: Bearer`. 프로젝트 소유자만 쓸 수 있고, `targetId` 는 서비스에 연결된 타깃이어야 한다.

### `GET /api/v1/services/{serviceId}/console?targetId=1`

콘솔을 열 수 있는지 알려 준다(콘솔 화면의 빈 상태 판단용). ticket·발급 기록을 만들지 않는다.

```json
{"success":true,"data":{"available":true}}
{"success":true,"data":{"available":false,"reason":"NO_RUNNING_DEPLOYMENT"}}
```

| `reason` | 의미 |
|---|---|
| `TARGET_NOT_SUPPORTED` | 콘솔을 지원하지 않는 타깃 종류다(AWS·온프레미스가 아닌 경우. 지금은 나오지 않는다) |
| `TARGET_NOT_CONNECTED` | 사용자가 등록한 온프레미스 서버가 연결돼 있지 않다(`CONNECTED` 가 아니다. 하트비트가 끊긴 `DISCONNECTED` 포함) |
| `NOT_CONFIGURED` | Control API 에 콘솔 설정(`CONSOLE_*`)이 없다 |
| `NO_RUNNING_DEPLOYMENT` | 그 타깃에 떠 있는 release 가 없다(배포 전, 또는 서비스를 내렸다) |

판정 순서는 타깃 종류 → 설정 → 서버 연결 → release 다. 에러: `401` · `404`(`SERVICE_NOT_FOUND`·`NOT_FOUND` — 남의 서비스와 연결되지 않은 타깃 포함) · `422`.

### `POST /api/v1/services/{serviceId}/console/sessions` → `201`

```json
// 요청
{"targetId":1}
// 응답
{"success":true,"data":{
  "sessionId":"3f0c9d3a-8f1e-4d57-9c34-6a1f2b7e5d10",
  "token":"eyJhbGciOiJFZERTQSIs…",
  "expiresAt":"2026-10-04T12:00:30.123456Z",
  "gateway":{"httpUrl":"https://api.likelion.uk","wsUrl":"wss://api.likelion.uk"}
}}
```

- `token` 은 60초짜리 ticket 이다. **Pod 목록 조회용과 연결용으로 매번 새로 받는다.** 연결(WebSocket `auth`)에는 한 번만 쓸 수 있다.
- `gateway.httpUrl`·`gateway.wsUrl` 은 끝의 `/` 가 없는 base 다. `{httpUrl}/v1/pods`, `{wsUrl}/v1/exec?pod=…` 로 붙는다.
- 에러: `401` · `404` · `409 NO_RUNNING_DEPLOYMENT` · `409 TARGET_NOT_CONNECTED`(등록한 온프레미스 서버가 `CONNECTED` 가 아니다. 배포 요청의 같은 이름 오류와 같은 기준) · `409 CONSOLE_TARGET_NOT_SUPPORTED` · `422` · `503 NOT_CONFIGURED`.
- 발급할 때마다 `console_sessions` 감사 행(누가·언제·서비스·타깃·release)이 남는다. 셸 입출력은 어디에도 남지 않는다.

## 2. Console Gateway REST

### `GET {httpUrl}/v1/pods`

헤더 `Authorization: Bearer <token>`. CORS 는 허용된 Origin(웹 주소)에만 열려 있고 쿠키는 쓰지 않는다.

```json
{"success":true,"data":{"pods":[
  {"name":"app-6d9f7c-abcde","phase":"Running","ready":true,"startedAt":"2026-10-04T11:00:00Z","releaseId":123}
]}}
```

- 서비스 namespace 의 Pod 중 컨테이너 `app` 이 있는 것만, `startedAt` 내림차순이다. `releaseId` 는 Pod 라벨 `iris/release-id` 이고 모르면 빠진다(온프레미스는 항상 빠진다).
- `phase` 는 Pod 단계(`Pending`·`Running`…)이고, 삭제 중이면 `Terminating` 이다. **`ready=true` 인 Pod 에만 연결할 수 있다**(`Running` 이면서 `app` 컨테이너가 준비된 경우).
- 이 호출은 ticket 을 소비하지 않는다(만료 전까지 여러 번 가능).
- 에러: `401 UNAUTHORIZED`·`401 TOKEN_EXPIRED` · `502 CLUSTER_UNAVAILABLE`.

## 3. Console Gateway WebSocket

`GET {wsUrl}/v1/exec?pod={name}` 로 업그레이드한다. `pod` 가 없으면 연결이 거절된다. 브라우저의 `Origin` 이 허용 목록에 없으면 업그레이드가 `403` 으로 거절된다.

모든 프레임은 JSON **텍스트** 프레임이다(바이너리·해석할 수 없는 프레임은 무시한다). 연결하면 **5초 안에** `auth` 를 보내야 한다. ticket 은 URL 이 아니라 이 프레임으로 보낸다.

클라이언트 → Gateway

```json
{"type":"auth","token":"<ticket>","cols":80,"rows":24}
{"type":"input","data":"ls -la\r"}
{"type":"resize","cols":120,"rows":30}
{"type":"ping"}
```

Gateway → 클라이언트

```json
{"type":"ready","pod":"app-6d9f7c-abcde","shell":"bash"}
{"type":"output","data":"…"}
{"type":"pong"}
{"type":"exit","code":0}
{"type":"error","code":"SHELL_NOT_FOUND","message":"이 이미지에는 셸이 없어요"}
```

- `auth` 의 `cols`·`rows` 는 처음 터미널 크기다(기본 80×24, 1~1000). `input.data` 는 최대 65536자다.
- `ready` 이후부터 입출력이 오간다. `shell` 은 `bash`(있으면) 또는 `sh` 다. 셸을 클러스터가 고르는 온프레미스에서는 `shell` 이 빠진다.
- `output.data` 는 TTY 의 stdout+stderr 를 UTF-8 로 푼 문자열이다(깨진 바이트는 `�`). 줄바꿈은 터미널 규칙(`\r\n`)을 그대로 따른다. xterm.js 에 그대로 쓴다.
- `exit.code` 는 셸 종료 코드다. 알 수 없으면(연결이 상태 없이 끊김) 필드가 없다. `exit` 뒤에 Gateway 가 소켓을 닫는다.
- `error` 를 보낸 뒤에도 Gateway 가 소켓을 닫는다. **화면은 숫자 close code 가 아니라 `error.code` 로 분기한다.** `message` 는 한국어 기본 문구이고 화면이 `code` 별로 바꿔 써도 된다.
- 유휴 연결이 끊기지 않게 **25초마다 `ping`** 을 보낸다(ALB idle timeout 60초). Gateway 도 uvicorn 기본 WebSocket ping(20초)을 보낸다.
- 입력 없이 15분이면 `IDLE_TIMEOUT`, 연결한 지 1시간이면 `MAX_DURATION_EXCEEDED` 로 끊는다. `ping`·`pong` 은 입력이 아니다.
- 한 사용자의 동시 연결은 3개(`SESSION_LIMIT_EXCEEDED`). Gateway replica 안에서 센다.

### 오류 코드 (`error.code`, REST 오류 본문의 `code` 도 같다)

| 코드 | 언제 | 화면 안내 예 |
|---|---|---|
| `UNAUTHORIZED` | ticket 이 없거나 서명이 틀리다. `auth` 가 5초 안에 오지 않았거나 첫 프레임이 `auth` 가 아니다 | 다시 연결 |
| `TOKEN_EXPIRED` | ticket 이 만료됐다(60초) | 새 ticket 을 받아 다시 연결 |
| `TOKEN_REUSED` | 이미 연결에 쓴 ticket 이다 | 새 ticket 을 받아 다시 연결 |
| `POD_NOT_FOUND` | 그 Pod 가 없다(교체됐을 수 있다) | Pod 목록을 새로 고침 |
| `POD_NOT_READY` | Pod 가 `Running` 이 아니거나 `app` 컨테이너가 준비되지 않았다 | 잠시 뒤 다시 시도 |
| `SHELL_NOT_FOUND` | 이미지에 `/bin/sh` 가 없다(distroless 등) | 이 이미지는 콘솔을 쓸 수 없다 |
| `SESSION_LIMIT_EXCEEDED` | 동시 연결 한도 | 열려 있는 콘솔을 닫기 |
| `IDLE_TIMEOUT` | 입력 없이 15분 | 다시 연결 |
| `MAX_DURATION_EXCEEDED` | 연결 후 1시간 | 다시 연결 |
| `CLUSTER_UNAVAILABLE` | 클러스터 API 에 닿지 못했다 | 잠시 뒤 다시 시도 |
| `INTERNAL_ERROR` | 그 밖의 오류 | 다시 시도 |

### 브라우저 예

```ts
// 1) 가능 여부 → 2) Pod 목록용 ticket → 3) Pod 목록 → 4) 연결용 새 ticket → 5) WebSocket
const { data: availability } = await get(`/api/v1/services/${id}/console?targetId=${targetId}`)
if (!availability.available) return showEmpty(availability.reason)

const list = await post(`/api/v1/services/${id}/console/sessions`, { targetId })
const pods = (await (await fetch(`${list.data.gateway.httpUrl}/v1/pods`, {
  headers: { Authorization: `Bearer ${list.data.token}` },
})).json()).data.pods.filter((pod) => pod.ready)

const { data: session } = await post(`/api/v1/services/${id}/console/sessions`, { targetId })
const ws = new WebSocket(`${session.gateway.wsUrl}/v1/exec?pod=${encodeURIComponent(pods[0].name)}`)
ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', token: session.token, cols: term.cols, rows: term.rows }))
ws.onmessage = ({ data }) => {
  const frame = JSON.parse(data)
  if (frame.type === 'output') term.write(frame.data)
  if (frame.type === 'error') showError(frame.code)
}
term.onData((data) => ws.send(JSON.stringify({ type: 'input', data })))
term.onResize(({ cols, rows }) => ws.send(JSON.stringify({ type: 'resize', cols, rows })))
setInterval(() => ws.readyState === WebSocket.OPEN && ws.send('{"type":"ping"}'), 25_000)
```

## 4. 운영

환경변수는 [설정](configuration.md)의 "Console Gateway" 절과 Control API 표에 있다.

- Control API: `CONSOLE_TICKET_PRIVATE_KEY`(Ed25519 PEM), `CONSOLE_GATEWAY_HTTP_URL`, `CONSOLE_GATEWAY_WS_URL`. 셋 중 하나라도 없으면 콘솔이 꺼진다.
- Console Gateway: `uvicorn app.console_gateway.main:app --host 0.0.0.0 --port 8080`. 헬스 `GET /healthz`·`GET /readyz`(204). **replica 는 1** 이다(1회용 ticket 검사가 메모리 기반, [ADR 0033](adr/0033-service-console-via-console-gateway.md)). 문서 화면(`/docs`)은 열지 않는다.
- 키 쌍(값은 어디에도 적지 않는다): `openssl genpkey -algorithm ED25519 -out console-ticket.pem` → 개인키는 Control API 에, `openssl pkey -in console-ticket.pem -pubout` 의 결과는 Gateway 에 넣는다.
- 감사 기록: Control API 의 `console_sessions`(발급)와 Gateway 구조화 로그의 `console_session_started`·`console_session_ended`(`session_id`·`user_id`·`service_id`·`pod`·`end_reason`·`duration_seconds`). 셸 입출력은 남기지 않는다.

## 온프레미스 타깃 (ADR 0035)

Gateway 가 Argo CD 의 터미널을 중계한다. 화면 코드는 AWS 와 같고, 다음 차이만 안다.

- **가능 여부**: 사용자가 등록한 서버는 `CONNECTED` 여야 한다. 아니면 `reason=TARGET_NOT_CONNECTED`(조회)·`409 TARGET_NOT_CONNECTED`(발급)다. 서버가 다시 연결되면(하트비트가 돌아오면) 저절로 열린다. 공용 `onprem` 타깃은 release 조건만 본다.
- **Pod 목록**: `releaseId` 가 없다. `ready` 는 Argo CD 가 본 상태(`Healthy` 이고 `Running`)다.
- **`ready` 프레임**: `shell` 이 없다. Argo CD 가 `bash`·`sh` 순으로 시도해 고른다.
- **`exit` 프레임**: `code` 가 없다. Argo CD 는 셸 종료 코드를 알려 주지 않는다.
- **`SHELL_NOT_FOUND`**: 이미지에 셸이 없을 때 외에도, Argo CD 가 셸을 못 열고 곧바로 연결을 닫은 모든 경우다. 서버의 Argo ServiceAccount 에 `pods/exec` 권한이 아직 없는 서버(기능 도입 전에 등록한 서버)도 같은 코드로 보인다. 화면은 "이 이미지에는 셸이 없어요" 외에 다른 원인이 있을 수 있다는 점을 숨기지 않는 문구를 쓰는 편이 안전하다.
- **`CLUSTER_UNAVAILABLE`**: Argo CD 터미널이 꺼져 있거나(`exec.enabled`) Gateway 의 Argo CD 설정·권한이 없다. 운영자가 고칠 일이다.
- **Argo CD 가 입력을 받는 방식**: 연결 직후 첫 출력(프롬프트)을 기다려(최대 5초) `ready` 를 보낸다. 프롬프트를 내지 않는 셸은 5초 뒤에 `ready` 가 온다.

