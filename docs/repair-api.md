# 코드수정 운영 API 명세

운영 Base URL은 `https://api.likelion.uk`, 공통 경로는 `/api/v1`이다. 전체 명세는 [Swagger UI](https://api.likelion.uk/docs), [ReDoc](https://api.likelion.uk/redoc), [OpenAPI JSON](https://api.likelion.uk/openapi.json)에서 확인한다. 저장소의 [openapi.json](openapi.json)은 같은 FastAPI 정의에서 생성한다.

## 인증과 공통 응답

웹은 `anydeploy_session` 쿠키, CLI/신뢰된 수정 코디네이터는 `Authorization: Bearer <WAS_SESSION_TOKEN>`을 사용한다. 이 토큰은 **WAS 세션**이며 GitHub PAT가 아니다. 응답 JSON 필드는 camelCase이고 성공은 `{"success":true,"data":...}`, 실패는 `{"success":false,"code":"...","message":"..."}` 형식이다. 필드 검증 실패에는 `details`가 추가될 수 있다.

CLI는 아래 기존 로그인 API로 WAS 세션을 받는다.

1. `POST /api/v1/auth/cli/sessions` — 인증 없이 세션 생성. `sessionId`, `pollSecret`, `verificationUrl`, `expiresIn`, `interval` 반환.
2. `verificationUrl`을 브라우저에서 열어 GitHub 로그인 승인. WAS는 사용자와 App 설치 연결을 저장한다. 로그인용 사용자 OAuth 토큰은 저장하지 않는다.
3. `POST /api/v1/auth/cli/sessions/{sessionId}/token`에 `{"pollSecret":"<poll-secret>"}` 전송. `interval`보다 빠르게 폴링하면 429. `APPROVED` 응답의 `accessToken`은 처음 한 번만 반환한다.

`pollSecret`, `accessToken`, 아래 GitHub 설치 토큰은 문서 예시·로그·작업 기록·모델 입력에 실제 값을 넣지 않는다.

## 서비스용 GitHub 쓰기 토큰

```http
POST /api/v1/services/{serviceId}/repair-github-token
Authorization: Bearer <WAS_SESSION_TOKEN>
Content-Type: application/json

{"repository":"owner/repository"}
```

| 요청 필드 | 계약 |
|---|---|
| `serviceId` | 로그인 사용자가 소유한 서비스 ID |
| `repository` | 필수 문자열, 3–201자, `owner/repo` 형식. 현재 서비스의 source repository와 대소문자 무시 비교하여 일치해야 함 |

WAS가 서비스 소유권, 저장소 일치, 로그인 시 연결된 App 설치와 현재 저장소 접근을 검사한다. 설치 ID와 권한은 클라이언트가 지정하지 않는다. WAS는 기존 소스 App 키로 서비스 저장소 하나에 `contents: write`, `pull_requests: write` 설치 토큰을 요청한다. GitOps App 키는 사용하지 않는다.

성공 예시는 실제 자격증명을 포함하지 않는다.

```http
HTTP/2 200
Cache-Control: no-store
Pragma: no-cache
Content-Type: application/json

{
  "success": true,
  "data": {
    "repository": "owner/repository",
    "token": "<short-lived-installation-token>",
    "expiresAt": "2030-01-01T01:00:00Z"
  }
}
```

`expiresAt`은 GitHub가 반환한 만료 시각이다. 코디네이터는 메모리에만 토큰을 보관하고 만료 60초 전에 같은 API로 갱신한다. GitHub 401이 발생하면 캐시를 비우고 다음 작업 재시도 때 재발급한다. 응답이 유실된 GitHub 쓰기는 인증 계층에서 자동 재전송하지 않는다.

| HTTP | code | 의미와 대응 |
|---|---|---|
| 401 | `UNAUTHORIZED` | WAS 로그인 없음/세션 만료. 기존 로그인으로 새 세션 발급. App 자격증명이 GitHub에서 거부된 경우도 401일 수 있어 서버 설정도 확인 |
| 403 | `REPOSITORY_NOT_ACCESSIBLE` | 사용자와 설치 연결이 없거나 해당 저장소가 설치에 허용되지 않음. App 설치와 저장소 선택 확인 |
| 403 | `FORBIDDEN` | App 또는 설치의 Contents·Pull requests 쓰기 권한 부족. 두 권한을 읽기/쓰기로 설정하고 설치에서 업데이트 승인 |
| 404 | `SERVICE_NOT_FOUND` | 서비스 없음 또는 다른 사용자 소유. 서비스 존재 여부를 타 사용자에게 공개하지 않음 |
| 409 | `CONFLICT` | 요청 repository가 현재 서비스 소스 저장소와 다름. 현재 서비스 정보를 다시 조회 |
| 422 | `VALIDATION_ERROR` | repository 누락/형식·길이 오류 |
| 502 | `EXTERNAL_ERROR` | GitHub 요청 실패, 잘못되거나 만료된 토큰 응답 등 외부 오류 |
| 503 | `NOT_CONFIGURED` | WAS의 GitHub App ID/개인키 또는 세션 설정 없음 |

쓰기 권한이 없을 때의 실제 응답 형식:

```json
{
  "success": false,
  "code": "FORBIDDEN",
  "message": "github app requires Contents and Pull requests write"
}
```

App 등록에 **Repository permissions → Contents: Read and write, Pull requests: Read and write**가 필요하며 기존 설치에서도 변경된 권한을 승인해야 한다. 기존 `GET /api/v1/github/install`은 설치 페이지로 302 이동한다. `GET /api/v1/github/installations`, `GET /api/v1/github/repos/resolve?url=`로 로그인 사용자에게 연결된 설치/저장소를 확인한다.

## 진단과 코드수정 후보

다음 경로는 모두 서비스 소유자의 WAS 인증을 요구한다.

| 메서드·경로 (`/api/v1` 뒤) | 용도 |
|---|---|
| `GET /services/{serviceId}/deployments/{deploymentId}/diagnosis` | 저장된 최근 진단 조회. 성공한 진단의 ID와 선택할 remediation plan ID 확인 |
| `GET /services/{serviceId}/deployments/{deploymentId}/repair-context?diagnosisId={diagnosisId}` | 특정 원본 진단과 실패 당시 source SHA/root, 단기 snapshot URL, archive/manifest 해시 조회. 응답 no-store |
| `POST /services/{serviceId}/deployments/{deploymentId}/repairs` | 특정 진단과 계획으로 수정 후보 생성 접수 |
| `GET /services/{serviceId}/repairs/{repairId}` | 후보 생성 상태와 결과 조회 |
| `GET /services/{serviceId}/repairs/{repairId}/artifacts/{name}` | 해시·크기를 검증한 `patch.diff`, `changes.json`, `manifest.json` 다운로드 |

후보 생성 요청:

```http
POST /api/v1/services/{serviceId}/deployments/{deploymentId}/repairs
Authorization: Bearer <WAS_SESSION_TOKEN>
Idempotency-Key: repair-incident-001
Content-Type: application/json

{"diagnosisId":123,"planIds":["plan-1"]}
```

`diagnosisId`는 해당 실패 배포의 성공한 원본 진단이어야 하고, `planIds`는 그 진단에서 실제 선택할 계획 ID다. 임의 ID는 거부하며, 계획 목록이 비어 있으면 유효한 계획을 선택할 수 없다. 설정 변경 계획만 선택한 경우 생성 단계가 설정 변경 결과를 반환할 수 있으므로 코드 후보가 생겼다고 가정하지 않는다. `Idempotency-Key`는 1–128자, 영문/숫자로 시작하는 영문·숫자·`_`·`-` 문자열이다. 같은 키와 입력은 기존 작업을 반환하며 입력이 바뀌면 409다. 신규 작업은 202, 기존 작업 재조회는 200이며 `data.id`로 폴링한다.

| 작업 status | 의미 |
|---|---|
| `RUNNING` | 생성 중. 같은 repair ID 조회 |
| `SUCCEEDED` | 후보 생성 단계 완료. `result.status`가 `candidate_ready`인지, 설정 변경/근거 부족 등의 다른 완료 결과인지 확인 |
| `FAILED` | 생성 실패. `errorCode` 확인 |
| `UNKNOWN_OUTCOME` | 외부 호출 결과 불확실. GET은 저장된 receipt를 읽어 복구할 수 있음. 모델 POST를 무조건 재전송하지 않음 |

후보의 `validation.status=not_run`, `validation.owner=was`는 실행 검증이 끝나지 않았음을 뜻한다. `SUCCEEDED`가 GitHub 머지나 운영 배포 성공을 의미하지 않는다. artifact 응답은 JSON 봉투 대신 `application/octet-stream`과 다운로드 파일명을 반환한다.

## GitHub 머지와 배포 상태 확인

WAS 후보 API와 토큰 API는 GitHub 브랜치 생성·PR 머지·재배포를 실행하지 않는다. 별도 `iris-auto-repair` 코디네이터가 서비스별 WAS 인증을 사용해 `hotfix/iris/{requestId}` 브랜치와 main 대상 PR을 만들고 GitHub PR API로 머지한다. 코디네이터의 자동 머지는 서비스 source branch가 main이어야 하며, PR head/base 변경과 GitHub 보호 규칙을 확인한다. 코디네이터 완료 상태는 `MERGED`, `--draft-pr` 사용 시 `PR_OPENED`다.

머지 후 기존 push webhook에 따른 배포 요청을 `GET /api/v1/services/{serviceId}/deployments`로 확인하고, 해당 `sourceSha`의 배포 상세 `GET .../deployments/{deploymentId}`에서 `SUCCEEDED` 여부를 확인한다. 머지 성공과 배포 성공을 별도로 기록한다. API 운영 준비 상태는 인증 없이 `GET /readyz`의 204로 확인한다.

## 운영 점검 기록 — 2026-10-03

WAS main의 `074cdc843c8583fe827b8c78a5fc61c5a3e25ff0` 배포 후 API로 코드수정 경로 노출, `/readyz` 204, 기존 GitHub 로그인과 WAS 세션 발급을 확인했다. 토큰 발급은 403 `FORBIDDEN`: 당시 `2026-softbank-1/anydeploy-iris-dev` App은 Contents·Metadata read만 허용했다. 익명 401, 저장소 불일치 409, 없는 서비스 404도 확인했다. 권한 승인 뒤 토큰 발급·실제 GitHub 쓰기는 별도로 재검증해야 한다.
