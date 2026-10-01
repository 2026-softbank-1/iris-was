# 소스 분석과 서비스 소유의 빌더 선택

2026-10-01 팀 결정에 따라 Dockerfile이 없는 소스는 서비스/백엔드 담당자(김현겸)의 Railpack 빌드 경로로 인계한다. 분석기는 소스 근거와 빌더 추천을 반환한다. Dockerfile을 생성하거나 소스를 수정하지 않는다.

## 연결 위치와 책임

```text
BUILD Job / 고정 source_sha 확보
  → 기존 소스 수집 Client: worker-owned 디렉터리
  → BuildPreparationService
      → SubprocessAnalyzerBuildClient: 분석·빌더 추천·원본 소스 패키징
      → SHA/root/platform/요청한 builder/원본 manifest/아카이브 검증
  → 서비스 소유자가 builder와 필요한 설정을 확정
  → 기존 CodeBuild: Dockerfile 또는 고정 버전 Railpack + BuildKit
  → ECR digest·빌드 로그·단계 결과 기록
  → 검증된 결함만 로그 기반 재귀 개선 에이전트로 인계
```

`app/workers/build_worker.py`의 Job 선점 로직은 기존 TODO 상태다. 이 모듈과 CLI는 큐·S3 업로드·CodeBuild 시작·ECR push·개선 에이전트 호출을 구현하지 않는다. 분석 권고만으로 `service.builder`를 저장하거나 `StartBuild`를 호출하는 경로도 없다.

## 빌더 선택 계약 v2

| 요청·소스 | 결과 | 이후 책임 |
|---|---|---|
| builder 미지정, 기본 Dockerfile 있음 | `ready`, Dockerfile 추천, `decisionRequired=true` | 서비스가 선택 확정 |
| builder 미지정, Dockerfile 없음 | `ready`, Railpack 추천, `decisionRequired=true` | 서비스가 버전·옵션·지원 여부 검증 후 확정 |
| builder 미지정, 다른 Dockerfile 후보 존재 | `needs_input`, 경로 선택 요청 | 서비스가 대상 경로 확정 |
| 명시적 `railpack` | 선택 보존, 원본만 패키징 | 기존 Dockerfile이 있어도 Railpack 유지 |
| 명시적 `dockerfile`, 기본 Dockerfile 없음 | `needs_input` | Railpack으로 몰래 변경하지 않고 설정 해결 |
| 명시적 Dockerfile 경로 없음·유효하지 않음 | 검증 오류 | 선택한 경로 수정 |

`PrepareBuildRequest.builder=None`은 분석기 요청에서 `builder="auto"`로 변환된다. 추천 요청을 허용하는 것은 미확정 서비스의 배포를 허용한다는 뜻이 아니다. 기존 서비스 정책대로 실제 빌드 시작 전에는 확정된 `service.builder`가 필요하다.

분석기 wire schema는 `iris.build-preparation-request.v2` / `iris.build-preparation.v2`다. 응답 `builder`는 추천 결과이고, 서비스가 요청한 선택은 `buildHandoff.requestedBuilder`에 별도 보존한다.

```json
{
  "builder": "railpack",
  "status": "ready",
  "buildHandoff": {
    "owner": "service",
    "requestedBuilder": null,
    "recommendedBuilder": "railpack",
    "decisionRequired": true,
    "reasonCode": "dockerfile_absent"
  },
  "executionAuthorized": false
}
```

위 JSON은 핵심 필드 발췌다. `ready`는 분석 결과와 원본 아카이브 준비 상태다. Railpack의 언어 지원 확인·빌드 성공·테스트 성공·ECR push·배포 승인이 아니다. `decisionRequired`는 미지정 요청 또는 `needs_input`일 때 true다. 정상적인 Dockerfile 부재는 코드 결함이나 재귀 수정 요청이 아니다.

Railpack 응답의 `dockerfilePath`, `dockerfileOrigin`, `dockerfileSha256`은 null이며, 소스에 Dockerfile이 있으면 아카이브에는 원래 바이트가 그대로 남는다. `templateId`는 항상 null이다. v1 응답과 `allow_generation` 요청은 거부한다. v1의 생성 Dockerfile overlay 허용 정책은 폐기했다.

## 호출 예시

```python
client = SubprocessAnalyzerBuildClient(
    ["/opt/iris-analyzer/bin/python", "-m", "iris_analyzer.build.cli", "--request-stdin"],
)
result = await BuildPreparationService(client).prepare_build(
    PrepareBuildRequest(
        source_directory=source_root,
        output_directory=job_directory,
        source_sha=pinned_commit_sha,
        root_directory=service.root_directory or ".",
        builder=service.builder,
        dockerfile_path=service.dockerfile_path,
        platform=service.platform,
    )
)
```

클래스는 각각 `app.clients.analyzer_build_client`, `app.services.build_preparation_service`, `app.schemas.build_preparation`에 있다. 모든 빌더에서 분석기와 아카이브 검증을 통과해야 이 준비 모듈의 `ready`를 받을 수 있다. 실행 명령은 `BUILD_PREPARATION_ANALYZER_COMMAND` JSON argv 또는 CLI 옵션으로 주입한다. 기본 timeout 120초는 `BUILD_PREPARATION_TIMEOUT_SECONDS`로 설정한다. 분석기를 Worker 이미지의 별도 환경에 고정된 wheel로 설치한다.

```sh
uv run python -m scripts.prepare_build --request /worker/request.json \
  --analyzer-command-json '["/opt/iris-analyzer/bin/python","-m","iris_analyzer.build.cli","--request-stdin"]'
```

요청 파일과 CLI 출력은 WAS 내부 snake_case다. Client가 분석기 wire camelCase로 한 번 변환한다.

```json
{
  "source_directory": "/worker/source",
  "output_directory": "/worker/new-job",
  "source_sha": "0123456789012345678901234567890123456789",
  "root_directory": ".",
  "builder": null,
  "dockerfile_path": null,
  "platform": "linux/amd64"
}
```

## 독립 검증과 하자 인계

- SHA·root·platform·요청 builder를 보존한다. 추천과 요청·reason·decisionRequired의 일관성을 검사한다.
- 원본 소스의 포함 파일과 아카이브를 독립 비교한다. 바이트·실행 권한·manifest·근거 SHA가 일치해야 한다. 추가 Dockerfile을 포함해 어떤 생성 파일도 허용하지 않는다.
- `source/` 아래 정규화된 tar 경로만 허용한다. 경로 이탈·링크·중복·누락·추가를 차단하고 binary 자산을 보존한다. 인증 파일·`.env*`·호스트 의존성·`secrets/.secrets/credentials`는 제외한다.
- timeout·출력 상한·취소 시 부모의 종료 여부와 관계없이 프로세스 그룹을 종료한다. AWS/OpenAI/GitHub 자격증명을 자동 상속하지 않는다.
- 결함 인계는 고정 SHA, 서비스·실행 조건, 근거와 검증 결과, 비밀값을 제거한 로그, 재현 명령, 기대·실제 결과를 묶는다. AI 의심·단순 설정 누락·인증/인프라 실패는 코드 결함으로 승격하지 않는다. 수정 에이전트의 결과도 별도 SHA로 다시 분석·검증한 뒤 후속 빌드 판단에 사용한다.

로그 기반 재귀 개선 인계의 판정·반복 제한은 [분석기 설계 문서](https://github.com/2026-softbank-1/iris-code-analyzer-agent/blob/feat/preprocess-deployment-contracts/docs/remediation-handoff.md)를 따른다. 이 WAS 변경은 해당 에이전트를 실행하지 않는다.

기존 [v1 빌드 준비 평가](https://github.com/2026-softbank-1/iris-code-analyzer-agent/blob/feat/preprocess-deployment-contracts/reports/build-preparation-validation.md)는 생성 템플릿의 과거 검증 기록이다. v2 Railpack 실행 성공 증거로 재사용하지 않는다.
