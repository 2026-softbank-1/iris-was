# 배포 상세 화면 API

배포 상세 화면(`/project/{projectId}/service/{serviceId}/deployment/{deploymentId}/details`)의 탭 4개가 쓰는 API 다. 모두 `/api/v1/services/{serviceId}/deployments/{deploymentId}` 아래에 있고, 로그인한 소유자만 접근한다(남의 서비스·없는 배포는 `404`).

| 탭 | 엔드포인트 | 데이터 소스 |
|---|---|---|
| Details | `GET /` (기존 상세 응답에 필드 추가) | DB |
| Build Logs | `GET /build-logs` | CodeBuild 로그(CloudWatch Logs) |
| Deploy Logs | `GET /deploy-logs` | Loki 앱 컨테이너 로그 + `iris_release_id` 라벨 |
| Network Logs | `GET /network-logs` | Loki ALB 접근 로그(`job="iris-alb-access"`) |

서비스 단위 로그·메트릭(`/services/{serviceId}/logs`·`/metrics`)은 [observability-api.md](observability-api.md) 다. 배포 로그는 그 위에서 이 배포의 release 로 거른 것이다.

## Details

기존 필드(`status`·`history`·`stages` 등)는 그대로이고 아래가 추가됐다.

| 필드 | 설명 |
|---|---|
| `source.repository` · `source.branch` | `owner/repo`, 서비스에 지정된 배포 브랜치(배포 시점이 아닌 현재 값). 커밋 SHA·메시지는 `sourceSha`·`sourceCommitMessage` |
| `configuration.build` | `builder`(없으면 생략 = 화면의 Auto-detect), `rootDirectory`(없으면 저장소 루트), `buildCommand` |
| `configuration.deploy` | `targets[]`(`id`·`name`·`kind`), `port`, `startCommand` |
| `build` | `status`·`builder`·`imageDigest`·`startedAt`·`finishedAt`·`failureCode`. 빌드 전에 끝난 요청은 없다 |
| `releases[]` | 타깃별 `id`·`targetId`·`status`·`argoSyncStatus`·`argoHealthStatus`·`gitopsCommitSha`·`failureCode`·`finishedAt`. 배포까지 가지 못한 요청은 빈 배열 |
| `replacedBy` | 성공했던 배포를 더 새로운 성공 배포가 대신했을 때만 `{deploymentId, at}`. 화면의 Removed 표시 근거 |

- `configuration` 의 `rootDirectory`·`buildCommand`·`port` 는 배포 시점 스냅샷이 아니라 서비스의 **현재 설정값**이다. `startCommand` 는 빌드가 기록한 값이 있으면 그 값이다.
- `targets` 는 실제로 반영한 타깃이다. release 가 아직 없으면 서비스에 지정된 타깃이다.

```json
{
  "success": true,
  "data": {
    "id": 5, "serviceId": 1, "status": "SUCCEEDED", "triggerType": "RESTART", "sourceDeploymentId": 4,
    "sourceSha": "7e18ff9f6748cd162bd1be6488f1ae75162f7263",
    "sourceCommitMessage": "chore: AnyDeploy 웹훅 push 테스트", "isActive": false,
    "history": [], "stages": [],
    "source": {"repository": "Saccharine1211/railway-deploy-demo", "branch": "main"},
    "configuration": {
      "build": {"builder": "railpack", "rootDirectory": "apps/web"},
      "deploy": {"targets": [{"id": 1, "name": "aws", "kind": "AWS"}], "port": 3000}
    },
    "build": {"status": "SUCCEEDED", "builder": "railpack", "imageDigest": "sha256:3f1c…"},
    "releases": [{"id": 7, "targetId": 1, "status": "SUCCEEDED", "argoSyncStatus": "Synced", "argoHealthStatus": "Healthy"}],
    "replacedBy": {"deploymentId": 6, "at": "2026-10-02T14:31:02.120Z"}
  }
}
```

## Build Logs

`GET /build-logs?cursor=&limit=500`

처음부터 `limit`(1~1000)개를 시간 오름차순으로 돌려주고 `nextCursor` 를 준다. 그 값을 `cursor` 로 다시 호출해 이어 읽는다.

```json
{"success":true,"data":{"entries":[{"timestampNs":"1790812800123000000","message":"[Container] Running command npm ci"}],"nextCursor":"f/391…","buildStatus":"BUILDING","isComplete":false,"isPartial":false,"loggedDeploymentId":5}}
```

- 진행 중인 빌드는 `isComplete` 가 `true` 가 될 때까지 `nextCursor` 로 폴링한다. `isComplete` 는 **빌드가 끝났고 이번 호출에서 읽은 로그가 없을 때** `true` 다. 끝난 빌드를 한 번에 받으려면 `entries` 가 빈 배열이 될 때까지 이어 호출한다.
- 롤백·재시작은 빌드를 새로 하지 않는다([ADR 0015](adr/0015-rollback-and-restart-reuse-built-image.md)). 이 요청은 `sourceDeploymentId` 를 따라가 실제로 빌드한 배포의 로그를 돌려주고, `loggedDeploymentId` 가 그 배포 id 다.
- CodeBuild 가 아직 시작하지 않았거나 빌드가 없으면 `entries` 는 빈 배열이고 `loggedDeploymentId` 가 없다. 방금 시작한 빌드는 로그 스트림이 몇 초 늦게 생긴다. 이때도 빈 배열이다.
- **CloudWatch 를 읽도록 설정되지 않은 환경**(`AWS_REGION`·`BUILD_LOG_GROUP` 없음)에서는 Build Worker 가 실패한 빌드에 남긴 끝부분(`builds.log_tail`, 최근 200줄·64KB, 비밀 패턴은 가림)만 돌려준다. 이때 `nextCursor` 는 없고 `isComplete` 는 `true` 이며, 앞부분이 잘렸으면 `isPartial` 이 `true` 다. 저장된 끝부분도 없으면(성공한 빌드 등) `503 NOT_CONFIGURED` 다.
- 로그 시각 `timestampNs` 는 JS 정밀도 손실을 피하려고 문자열이다. 메시지의 끝 줄바꿈은 제거했다.
- CloudWatch 에서 읽은 로그는 비밀 패턴을 가리지 않는다(서비스 소유자가 자기 빌드 로그를 보는 용도다).
- 로그 검색·다운로드는 이 API 범위 밖이다. 화면은 읽어 온 줄에서 거른다.

## Deploy Logs

`GET /deploy-logs?targetId=&start=&end=&limit=200&search=`

서비스의 앱 컨테이너(`app`) 로그 중 이 배포의 release(Loki 라벨 `iris_release_id` = `releases.id`)가 붙은 것만 돌려준다. 응답은 서비스 로그 API 와 같은 `entries`·`isTruncated` 에 실제로 조회한 `start`·`end` 가 붙는다.

```json
{"success":true,"data":{"entries":[{"timestampNs":"1790812800000000000","message":"listening on 8080","pod":"app-abc-xyz","container":"app"}],"isTruncated":false,"start":"2026-10-02T14:21:44.390Z","end":"2026-10-02T14:31:02.120Z"}}
```

- `start`·`end` 는 선택이다. 없으면 배포가 `DEPLOYING` 이 된 시각부터 `replacedBy.at`(교체되지 않았으면 지금)까지다. 어느 쪽이든 최대 7일, 미래는 받지 않는다(`422`).
- `targetId` 는 선택이다. 없으면 이 배포가 반영된 첫 타깃이고, 배포가 쓰지 않은 타깃이면 `422`.
- release 가 없는 배포(빌드 실패 등)는 에러가 아니라 빈 `entries` 이고 `start`·`end` 가 없다.
- 최근 `limit`(1~1000)개를 시간 오름차순으로 돌려준다. `isTruncated` 가 `true` 이면 범위를 좁힌다. `search` 는 대소문자를 구분하는 부분 문자열이다.
- stdout/stderr 를 구분하는 수집 라벨이 없어 `stream` 필터는 없다.
- 실시간 SSE 는 이 배포 단위로는 제공하지 않는다. 진행 중인 배포는 `end` 를 늘려 가며 폴링한다.

## Network Logs

`GET /network-logs?targetId=&start=&end=&limit=200&statusClass=5xx`

ALB 접근 로그 중 이 서비스가 처리한 요청을 돌려준다.

```json
{"success":true,"data":{"entries":[{"timestampNs":"1790812801000000000","status":200,"targetStatus":200,"receivedBytes":80,"sentBytes":2048,"responseTimeSeconds":0.0123}],"isTruncated":false,"start":"2026-10-02T14:29:34.920Z","end":"2026-10-02T14:31:02.120Z"}}
```

| 필드 | 의미 |
|---|---|
| `status` | 사용자에게 돌려준 ALB 최종 응답 코드 |
| `targetStatus` | 서비스(Pod)가 돌려준 코드. ALB 가 직접 응답했으면 없다 |
| `receivedBytes` · `sentBytes` | ALB 가 받은 요청·보낸 응답 바이트(TCP/TLS 오버헤드 제외) |
| `responseTimeSeconds` | ALB 가 서비스에 요청을 보내고 응답 헤더를 받기까지. 클라이언트 체감 시간이 아니다. 없으면 측정되지 않은 것 |

- **배포 구분 라벨이 없다.** ALB 로그엔 어느 배포가 처리했는지가 없어, 이 배포가 서비스한 구간(`SUCCEEDED` 가 된 때부터 `replacedBy.at` 또는 지금까지)의 시간 범위로 나눈다. `start`·`end` 로 좁힐 수 있다. 서비스한 적 없는 배포(성공하지 못한 배포)는 빈 `entries` 이고 `start`·`end` 가 없다.
- **URL·메서드·IP·User-Agent 는 없다.** 수집기가 개인정보 때문에 Loki 로 보내지 않는다. 경로가 필요하면 수집기 정책을 바꿔야 한다(인프라·보안 결정).
- `statusClass` 는 `2xx`·`3xx`·`4xx`·`5xx` 다.
- **수집기가 배포되기 전에는 비어 있다.** 아래 운영 연결을 본다. 수집에도 지연이 있다. ALB 가 로그 파일을 약 5분 주기로 S3 에 올리고 수집기가 그것을 Loki 로 옮기므로 최근 몇 분은 비어 있을 수 있다(집계 지표의 15분 대기와는 다른 값이다. iris-infra `contracts/service-traffic.md`).
- ALB 접근 로그는 best effort 다. 청구·정산 용도로 쓰지 않는다.

## 운영 연결

- Deploy Logs: `LOKI_URL` 만 있으면 된다([observability-api.md](observability-api.md)). `iris_release_id` 라벨이 모든 로그에 붙는지는 배포 후 확인한다.
- Network Logs: `LOKI_URL` + iris-infra 의 ALB 로그 수집기(`feat/alb-log-to-loki`, `albTraffic.enabled`, 이미지 digest)가 배포돼야 한다. 그 전에는 빈 배열이다.
- Build Logs: 로그 전체를 읽으려면 Control API 에 두 값이 필요하다. 없으면 위의 저장된 끝부분으로 대신한다.

  ```dotenv
  AWS_REGION=ap-northeast-2
  BUILD_LOG_GROUP=/aws/codebuild/iris-dev-build
  ```

  Control API 의 IAM Role 에 그 로그 그룹의 `logs:GetLogEvents` 가 필요하다. iris-infra [PR #46](https://github.com/2026-softbank-1/iris-infra/pull/46) 이 `control-api` 역할(`iris-platform/iris-platform-api` Pod Identity)과 차트 값 `api.buildLogGroup`(→ 위 두 환경변수)을 추가한다. 적용 뒤 API Pod 는 재시작돼야 자격 증명을 받는다. 로그 스트림은 CodeBuild build id(`{project}:{uuid}`)의 uuid 다. 읽기 전용 호출이다([ADR 0021](adr/0021-deployment-detail-logs-api.md)). Build Worker 의 로그 읽기 권한([ADR 0020](adr/0020-ai-error-diagnosis-via-agent-server.md))과는 별개의 Role 이라 각각 필요하다.

## 검증 범위

`tests/` 가 DB 없이 검증한다: Details 필드 조립·교체 배포·롤백 빌드 로그 사슬(`test_deployment_history_service.py`·`test_deployment_log_service.py`), CloudWatch 읽기(botocore Stubber, `test_build_log_reader.py`)·저장된 끝부분 대체 조회(`test_build_log_fallback.py`), Loki 쿼리·ALB 본문 파싱(MockTransport, `test_network_log_client.py`), 라우터 응답·에러·세션 반납(`test_deployment_logs_router.py`).

**실제 백엔드로는 검증하지 못했다.** CodeBuild 로그 스트림 이름 규칙(uuid)·`iris_release_id` 라벨·ALB 수집 스트림은 문서와 코드를 근거로 했다. 배포 후 `/build-logs`·`/deploy-logs`·`/network-logs` 를 실제 배포 한 건으로 호출해 확인한다.
