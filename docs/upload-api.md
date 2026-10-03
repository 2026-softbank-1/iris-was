# 소스 업로드 API (`likelion up`)

`likelion up` 이 현재 폴더를 tar.gz 로 올리고 `CLI` 배포 요청으로 배포하는 서버 쪽 계약이다. CLI 와 맞춘 원본은 iris-cli 레포의 `docs/up-contract.md` 이고, 이 문서는 서버가 실제로 하는 동작을 적는다. 설계 배경은 [ADR 0023](adr/0023-cli-source-upload-storage-and-archive-defense.md).

```text
CLI                                   Control API                      Build Worker
 │ POST /services/{id}/uploads         │ 소유·크기·gzip 확인, S3 저장
 │◀── 201 uploadId, sizeBytes … ───────│
 │ POST /services/{id}/deployments     │ 업로드를 요청에 묶음(원자적)
 │   { triggerType: CLI, uploadId }    │ → QUEUED, BUILD job
 │◀── 배포 요청(sourceSha=upload-…) ────│
 │ GET /services/{id}/deployments/{id} │                                 업로드를 받아 검사·재패킹
 │   (끝날 때까지 폴링)                  │                                 → 스냅샷 → CodeBuild → 배포
```

모든 응답은 `ApiResponse` 봉투, JSON 키는 camelCase, 인증은 `Authorization: Bearer`(쿠키도 된다)다. API 문서(Swagger)에도 같은 내용이 있다.

## `POST /api/v1/services/{serviceId}/uploads`

본문이 곧 아카이브다(multipart 아님).

| 헤더 | 값 |
|---|---|
| `Content-Type` | `application/gzip` (서버는 값을 보지 않고 본문의 gzip 매직 바이트를 본다) |
| `Content-Length` | 필수. 본문 바이트 수 |

```http
POST /api/v1/services/12/uploads
Authorization: Bearer <token>
Content-Type: application/gzip
Content-Length: 123456

<tar.gz 바이트>
```

`201 Created`:

```json
{
  "success": true,
  "data": {
    "uploadId": "zIbvepJ3aBsJgNfcuzS9svmP1tKfhDNmlieHBeDwNzo",
    "sizeBytes": 123456,
    "sha256": "5cb0705a44947aa32ced0b261cd56cd5978bf3735fed8a5c65ab1ae7b618f259",
    "expiresAt": "2026-10-04T03:29:29.796365Z"
  }
}
```

- `uploadId` 는 추측할 수 없는 256비트 값이다. `sha256` 은 서버가 받은 바이트의 해시라 CLI 가 보낸 값과 맞춰 볼 수 있다.
- 본문은 메모리에 올리지 않고 임시 파일로 받는다.

| 상태 | `code` | 언제 |
|---|---|---|
| `401` | `UNAUTHORIZED` | 로그인하지 않았다 |
| `404` | `SERVICE_NOT_FOUND` | 로그인한 사용자의 서비스가 아니다(없는 서비스와 구분하지 않는다) |
| `413` | `UPLOAD_TOO_LARGE` | `Content-Length` 가 한도(기본 250MB, `UPLOAD_MAX_BYTES`)를 넘는다. **본문을 읽기 전에** 거절한다 |
| `415` | `UPLOAD_NOT_GZIP` | 본문이 gzip(`1f 8b`)으로 시작하지 않는다. 첫 조각에서 끊는다 |
| `422` | `INVALID_INPUT` | `Content-Length` 없음(chunked)·0, 받은 바이트가 `Content-Length` 와 다름 |
| `502` | `EXTERNAL_ERROR` | S3 저장 실패. 다시 시도한다 |
| `503` | `NOT_CONFIGURED` | 서버에 `AWS_REGION`·`ARTIFACT_BUCKET` 이 없다 |

업로드는 `expiresAt`(올린 때부터 24시간)이 지나거나 배포 요청에 쓰이면 더 쓸 수 없다. 서버는 아카이브 **안의 내용을 읽지 않는다**. 경로·링크 검사는 빌드 때 한다(아래).

## `POST /api/v1/services/{serviceId}/deployments` — `CLI`

```json
{ "triggerType": "CLI", "uploadId": "<uploadId>" }
```

헤더 `Idempotency-Key` 는 기존과 같다. 같은 키로 다시 보내면(재시도) 업로드가 이미 쓰였더라도 처음 만든 요청을 그대로 돌려준다.

- `triggerType=CLI` 이면 `uploadId` 가 필수이고, 다른 트리거에서는 보낼 수 없다(`422 VALIDATION_ERROR`). `sourceSha`·`sourceDeploymentId` 도 함께 보낼 수 없다.
- 응답은 기존 `DeploymentResponse` 와 같다. `sourceCommitMessage` 는 없다.
- **`sourceSha` 는 `upload-` + 아카이브 sha256 의 앞 12자**다(예: `upload-3fa9c2d1b7e4`). Git 커밋 SHA 가 아니므로 GitHub 커밋 링크를 만들면 안 된다. 같은 내용을 올리면 같은 값이다. 앱의 `IRIS_GIT_COMMIT_SHA` 환경변수에도 이 값이 들어간다.

| 상태 | `code` | 언제 |
|---|---|---|
| `404` | `UPLOAD_NOT_FOUND` | 모르는 `uploadId` 이거나 다른 서비스의 업로드 |
| `409` | `UPLOAD_UNAVAILABLE` | 이미 배포 요청에 쓰였거나 만료됐다 |
| `409` | `DEPLOYMENT_IN_PROGRESS` | 진행 중인 배포가 있다. **업로드는 되돌아가** 다시 쓸 수 있다 |

한 업로드는 배포 요청 하나에만 묶인다. 동시에 여러 요청이 와도 하나만 이긴다.

## 빌드가 하는 일

Build Worker 는 `CLI` 요청의 소스를 GitHub 가 아니라 이 업로드에서 가져온다.

1. 업로드를 S3 에서 받아 크기·sha256 이 기록과 같은지 확인한다.
2. 항목마다 검사하며 GitHub tarball 과 같은 모양(최상위 디렉터리 하나)으로 다시 묶어 `snapshots/{buildId}.tar.gz` 에 둔다. buildspec 이 `tar -xz --strip-components=1` 로 풀기 때문이다.
3. 이후 CodeBuild·배포 단계는 GitHub 요청과 같다.

**아카이브의 루트가 서비스 소스의 루트다.** 서비스의 `rootDirectory` 는 저장소 안의 위치라 업로드에는 적용하지 않는다. `Dockerfile`·`iris.json` 은 아카이브 루트에서 찾는다. 모노레포의 `apps/web` 서비스라면 `apps/web` 폴더에서 `up` 한다.

아카이브가 아래에 걸리면 배포 요청이 `FAILED` 로 끝난다. 사용자에게는 `failureCode` 만 보이고, 원인 항목은 서버 로그에 남는다.

| `failureCode` | 원인 |
|---|---|
| `SOURCE_INVALID` | 손상된 tar.gz·올린 뒤 체크섬이 달라짐·절대 경로·`..` 가 든 경로·링크 아래의 항목·루트 밖이나 절대 경로를 가리키는 심볼릭 링크(링크 사슬 포함)·이미 나오지 않은 파일에 대한 하드 링크·장치/FIFO·중복 경로 |
| `SOURCE_TOO_LARGE` | 압축 크기가 `SNAPSHOT_MAX_BYTES` 를 넘음, 또는 풀었을 때 총 크기(2GiB)·항목 수(10만)·심볼릭 링크 수(5000)가 한도를 넘음 |
| `SOURCE_REF_NOT_FOUND` | S3 에서 업로드를 찾을 수 없음(보존 기간이 지났거나 지워짐). Build Worker 역할에 `s3:ListBucket` 이 없으면 없는 키도 `403` 이라 재시도 끝에 `BUILD_INFRA_ERROR` 가 된다 |
| `BUILD_CONFIG_REQUIRED` | 빌더 설정이 없거나 `Dockerfile` 이 없음(GitHub 소스와 같다) |

CLI 가 만드는 아카이브(`.git`·`node_modules`·`.likelion` 제외, 폴더 밖을 가리키는 링크 제외)는 모두 통과한다. 루트 안을 가리키는 `..` 링크(`src/config.json -> ../shared/config.json`)도 통과한다.

## 배포 이후

- **롤백·재시작**은 이미 만든 이미지를 쓰므로 `CLI` 로 만든 배포에도 그대로 된다. 새 요청의 `sourceSha` 는 원본(`upload-…`)을 따른다.
- **재배포(`REDEPLOY`)** 는 소스를 다시 받아 빌드하는데 업로드 아카이브는 S3 lifecycle(24~48시간)로 사라지므로 `CLI` 로 만든 배포는 `422 INVALID_INPUT`(`sourceDeploymentId`)이다. 다시 `likelion up` 한다.
- **AI 진단**은 같은 스냅샷을 쓰고 소스의 루트를 `.` 로 넘긴다. `sourceSha` 가 커밋이 아니므로 `commitSha` 는 보내지 않는다.

## 운영 설정

| 대상 | 필요한 것 |
|---|---|
| Control API | 역할 `iris-dev-control-api` 의 새 인라인 정책에 `uploads/*` 의 `s3:PutObject`·`s3:AbortMultipartUpload`(진단 소스 전송을 함께 켜므로 `snapshots/*` 의 `s3:GetObject` 도). 환경변수 `AWS_REGION`·`ARTIFACT_BUCKET` 은 운영 Secret `iris-platform-was-env` 로 넣고 API 를 롤링 재시작한다 |
| Build Worker | 역할에 `uploads/*` 의 `s3:GetObject`. `AWS_REGION`·`ARTIFACT_BUCKET` 은 이미 ConfigMap 으로 있어 재시작하지 않는다 |

두 환경변수가 업로드 API 와 진단의 소스 전송을 함께 켜므로 Control API 의 Role 권한을 **먼저** 적용한다. IAM 은 iris-infra 가 소유하고 인라인 정책 변경은 떠 있는 Pod 에도 바로 적용된다. 제안 diff 와 순서는 [ADR 0023](adr/0023-cli-source-upload-storage-and-archive-defense.md) 의 "인프라 변경".

## 웹 프런트에 미치는 영향

- `triggerType` 에 `CLI` 가 새로 나온다(배포 목록·서비스 카드의 `latestDeployment` 포함).
- `CLI` 배포의 `sourceSha`(`upload-…`)로 GitHub 커밋 링크를 만들지 않는다. `sourceCommitMessage` 가 없다.
- 새 `failureCode` 로 `SOURCE_INVALID` 가 나온다.
- `CLI` 로 만든 배포는 "재배포" 대신 "재시작"·"롤백"만 된다(재배포는 `422`).
