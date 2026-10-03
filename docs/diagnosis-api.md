# AI 에러 진단 API

실패한 배포를 에러 진단 에이전트(`iris-error-check-agent`)로 진단해 **원인과 해결책**을 받는다. 설계 근거는 [ADR 0020](adr/0020-ai-error-diagnosis-via-agent-server.md)이다.

- 진단하는 것은 배포의 **런타임 로그**(서비스 Pod 의 `app` 컨테이너)와, 가능하면 빌드가 올린 **소스 스냅샷**이다.
- 해결책은 **제안일 뿐** 서버가 실행하지 않는다. 진단 결과로 배포 요청의 상태도 바뀌지 않는다.
- 인증은 다른 API 와 같다(쿠키 또는 Bearer). 서비스 소유자만 쓸 수 있고 남의 서비스는 `404` 다.

## 흐름: 시작 → 폴링

모델 호출이 최대 150초 걸리고 ALB idle timeout 이 60초라서, 진단은 **시작과 조회를 나눈다.**

```text
POST .../diagnose   ──▶ 202  data.status = RUNNING   (진단은 서버가 이어서 실행)
GET  .../diagnosis  ──▶ 200  data.status = RUNNING   ← 2~3초마다 반복
GET  .../diagnosis  ──▶ 200  data.status = SUCCEEDED (analysis 있음) 또는 FAILED (errorCode 있음)
```

- 폴링은 2~3초 간격으로 `SUCCEEDED`·`FAILED` 가 될 때까지 한다. 보통 20~60초, 최대 150초쯤이다. `RUNNING` 이 4분을 넘으면 서버가 중간에 죽은 것이니 다시 시작한다(다시 `POST` 하면 서버가 먼저 낡은 `RUNNING` 을 `FAILED`(`DIAGNOSIS_ABANDONED`)로 닫는다).
- 탭을 닫았다 다시 열어도 `GET` 으로 이어서 볼 수 있다.

## 진단 시작

`POST /api/v1/services/{serviceId}/deployments/{deploymentId}/diagnose[?refresh=true]`

본문은 없다.

- `202` — 진단을 시작했다. 본문은 `status=RUNNING` 인 진단(아직 `analysis` 없음)이다.
- `200` — 이미 **성공한 진단이 있어** 모델을 다시 부르지 않고 그 결과(`status=SUCCEEDED`, `analysis` 있음)를 바로 돌려준다. 다시 진단하려면 `refresh=true`(모델 비용이 든다, 이때는 `202`). 이전 진단은 이력으로 남고 조회는 가장 최근 것을 본다.
- `FAILED`·`ROLLED_BACK`·`MANUAL_INTERVENTION` 배포만 진단한다. 그 밖의 상태는 `409 DEPLOYMENT_NOT_FAILED`.
- 같은 배포를 진단하는 중이면 `409 DIAGNOSIS_IN_PROGRESS`. 폴링으로 이어서 본다.

## 진단 조회

`GET /api/v1/services/{serviceId}/deployments/{deploymentId}/diagnosis`

가장 최근 진단을 `status` 와 상관없이 돌려준다. 진단한 적이 없으면 `404 DIAGNOSIS_NOT_FOUND`. 에이전트 설정이 없어도 조회는 된다.

## 응답

```json
{
  "success": true,
  "data": {
    "id": 3,
    "deploymentId": 12,
    "status": "SUCCEEDED",
    "analysis": {
      "analysisStatus": "diagnosed",
      "summary": "DATABASE_URL 이 없어 앱이 시작하지 못했다.",
      "observations": [
        { "id": "O1", "kind": "failure", "text": "필수 설정 누락", "evidenceIds": ["EV000001"] }
      ],
      "hypotheses": [
        {
          "id": "H1",
          "category": "configuration",
          "supportLevel": "direct",
          "statement": "DATABASE_URL 환경변수가 설정되지 않았다.",
          "observationIds": ["O1"],
          "evidenceIds": ["EV000001"],
          "counterEvidenceIds": [],
          "uncertainty": "다른 변수도 빠졌을 수 있다."
        }
      ],
      "nextChecks": [
        { "id": "C1", "target": "서비스 변수", "method": "DATABASE_URL 이 등록됐는지 본다.", "purpose": "누락 확인", "hypothesisIds": ["H1"] }
      ],
      "missingInformation": [],
      "limitations": ["소스를 보지 못했다."],
      "remediation": {
        "status": "proposed",
        "reason": "원인이 로그에 직접 나온다.",
        "plans": [
          {
            "id": "R1",
            "title": "DATABASE_URL 변수 추가",
            "hypothesisIds": ["H1"],
            "evidenceIds": ["EV000001"],
            "applyWhen": ["DATABASE_URL 이 서비스 변수에 없을 때"],
            "changes": [
              {
                "kind": "configuration",
                "target": "서비스 변수",
                "targetKnown": true,
                "instruction": "DATABASE_URL 을 추가한다.",
                "language": "dotenv",
                "snippetKind": "template",
                "snippet": "DATABASE_URL={{DATABASE_URL}}",
                "placeholders": [{ "name": "DATABASE_URL", "description": "DB 접속 문자열" }]
              }
            ],
            "verification": [{ "instruction": "다시 배포한다.", "expectedResult": "앱이 시작한다." }],
            "rollback": ["변수를 삭제한다."],
            "risks": ["잘못된 값이면 연결에 실패한다."]
          }
        ]
      }
    },
    "evidence": [
      { "id": "EV000001", "sourceId": "app-0", "stage": "runtime", "stream": "unknown", "timestamp": "2026-10-03T01:00:00Z", "eventLine": 1, "text": "ERROR Missing required configuration: DATABASE_URL" }
    ],
    "sourceAnalysis": { "status": "not_needed", "reason": "로그만으로 충분하다.", "findings": [], "evidence": [], "limitations": [] },
    "inputLimitations": [],
    "createdAt": "2026-10-03T01:21:00Z",
    "finishedAt": "2026-10-03T01:21:34Z"
  }
}
```

화면에 쓰는 법:

| 보여 줄 것 | 필드 |
|---|---|
| 한 줄 요약 | `analysis.summary` |
| **원인** | `analysis.hypotheses[]` — `statement`, 확신 정도 `supportLevel`(`direct`: 로그에 직접 나옴 · `supported`: 근거로 추정), 불확실한 점 `uncertainty` |
| **해결책** | `analysis.remediation.plans[]` — `title`, 적용 조건 `applyWhen`, 수정 예시 `changes[]`, 확인 방법 `verification[]`, 되돌리기 `rollback`, 주의 `risks` |
| 근거 로그 | `hypotheses[].evidenceIds`·`plans[].evidenceIds` 가 `evidence[].id` 를 가리킨다(마스킹된 로그 줄) |
| 더 확인할 것 | `analysis.nextChecks[]`, 부족한 정보 `analysis.missingInformation[]` |
| 한계 | `analysis.limitations`, `inputLimitations`(로그 누락·잘림·마스킹) |

- `analysis.analysisStatus` 는 `diagnosed`(원인 후보 있음) · `insufficient_evidence`(근거 부족) · `no_failure_evidence`(로그에 실패 흔적 없음)다. 뒤 둘은 정상 응답이고 `hypotheses`·`plans` 가 비어 있을 수 있다. `remediation.status` 는 `proposed`·`needs_more_evidence`·`not_needed` 다.
- 수정 예시(`changes[].snippet`)는 **템플릿**이다. `{{NAME}}` 자리표시자(`placeholders`)를 채워 써야 하고, 실제 코드를 열어 만든 패치가 아니다(`targetKnown` 은 대상 문자열이 로그에 있었다는 뜻일 뿐 맞다는 보장이 아니다).
- `sourceAnalysis` 는 소스까지 본 결과다(`not_needed`·`unavailable`·`analyzed`·`failed`). 소스를 못 읽어도 로그 진단은 유지된다. `findings[]` 는 파일·줄·연결된 원인(`hypothesisIds`)이다.
- `status=FAILED` 면 `analysis` 가 없고 `errorCode` 가 있다(아래 오류 표). 다시 `POST` 하면 새로 진단한다.
- 값이 없는 필드는 응답에서 빠진다.

## 오류

| 상태 | 코드 | 의미·화면 처리 |
|---|---|---|
| 401 | `UNAUTHORIZED` | 로그인 필요 |
| 404 | `SERVICE_NOT_FOUND` · `DEPLOYMENT_REQUEST_NOT_FOUND` | 서비스·배포가 없거나 남의 것 |
| 404 | `DIAGNOSIS_NOT_FOUND` | (조회) 아직 진단한 적 없음 → "AI 진단" 버튼 표시 |
| 409 | `DEPLOYMENT_NOT_FAILED` | 실패하지 않은 배포. 버튼을 실패한 배포에서만 보인다 |
| 409 | `DIAGNOSIS_IN_PROGRESS` | 진단 중. 잠시 뒤 조회 |
| 503 | `NOT_CONFIGURED` | 에이전트 설정 없음(운영 설정 문제) |

진단을 시작한 뒤의 실패는 응답 코드가 아니라 **폴링 결과의 `status=FAILED` 와 `errorCode`** 로 온다.

| errorCode | 의미·화면 처리 |
|---|---|
| `DIAGNOSIS_LOGS_UNAVAILABLE` | 진단할 로그가 없다. 아래 한계 참고 |
| `MODEL_TIMEOUT` · `MODEL_RATE_LIMIT` · `BUSY` 등 | 에이전트·모델 쪽 실패(시간 초과·사용량 한도·에이전트가 다른 진단 중). 다시 시도 |
| `EXTERNAL_ERROR` · `INVALID_RESPONSE` · `TIMEOUT` | 로그 백엔드·에이전트 호출 실패나 쓸 수 없는 응답. 다시 시도 |
| `DIAGNOSIS_ABANDONED` | 서버가 중간에 죽어 닫힌 진단. 다시 시도 |
| `INTERNAL_ERROR` | 예상 못 한 서버 오류. 다시 시도해도 계속되면 알린다 |

## 한계 (먼저 알아 둘 것)

- **빌드 단계 실패는 아직 진단하지 못한다.** CodeBuild 로그를 수집하지 않아 `DIAGNOSIS_LOGS_UNAVAILABLE` 이 된다. 이 경우 빌드 로그 링크(`log_url`)로 안내한다. 후속 작업으로 제안한 상태다.
- 앱이 뜨기 전에 실패해 앱 로그가 없는 배포(이미지 pull 실패 등)도 같은 이유로 진단하지 못한다.
- 소스 스냅샷은 빌드 뒤 하루만 남는다. 오래된 배포는 로그만으로 진단한다.
- 로그가 길면 가장 최근 줄만 보낸다. 이때 로그 범위가 불완전하다고 에이전트에 알리므로 결과의 한계(`limitations`·`inputLimitations`)에 반영될 수 있다.
- 환경변수 값은 에이전트에 보내지 않는다. 로그·소스는 에이전트가 비밀값 패턴을 가리지만, 모든 형식을 가린다는 보장은 없다.
