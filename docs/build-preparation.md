# 분석 뒤 Dockerfile 준비 단계

고정 소스를 CodeBuild에 넘기기 전에 `BuildPreparationService`를 호출한다. 기존 Dockerfile은 보존하고, 없는 경우 AI 분석 모듈의 검증된 생성 정책을 사용한다. 빌드·배포 전체 오케스트레이션과 DB 모델은 기존 설계를 따른다.

## 연결 위치

```text
BUILD Job / 고정 source_sha 확보
  → 기존 소스 수집 Client: worker-owned 디렉터리
  → BuildPreparationService
      → SubprocessAnalyzerBuildClient: 분석·Dockerfile 준비
      → 원본/아카이브/Dockerfile/manifest 검증
  → ready 아카이브를 S3에 업로드
  → 기존 CodeBuild: ROOT_DIRECTORY / DOCKERFILE_PATH로 이미지 빌드
  → ECR digest·로그·단계 시간을 기존 Build/Job에 기록
```

현재 `app/workers/build_worker.py`의 Job 선점 로직은 기존 TODO 상태다. 이 변경은 Worker가 호출할 준비 단계와 실행 가능한 검증 CLI를 제공한다. 큐·S3 업로드·CodeBuild 시작·ECR push를 완료했다고 주장하지 않는다. Control API가 빌드를 실행하는 경로도 추가하지 않는다.

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

위 클래스는 각각 `app.clients.analyzer_build_client`, `app.services.build_preparation_service`, `app.schemas.build_preparation`에 있다. 설치된 분석기 실행 명령은 `BUILD_PREPARATION_ANALYZER_COMMAND` JSON argv 또는 CLI 옵션으로 주입한다. timeout 기본값은 120초이며 `BUILD_PREPARATION_TIMEOUT_SECONDS`로 설정한다. shell 문자열은 사용하지 않는다. 분석기 패키지는 Worker 이미지의 별도 환경에 고정된 wheel로 설치하며, WAS의 일반 API 환경에 사설 Git 의존성을 강제로 추가하지 않는다.

`builder=None`은 기존 규칙대로 `BUILD_CONFIG_REQUIRED`다. `builder=railpack`이면 명시한 선택을 보존해 기존 Railpack 준비 경로로 넘긴다. `builder=dockerfile`일 때만 분석기 준비 단계를 호출한다. 기본 Dockerfile이 없으면 제한된 생성 프로파일을 적용하고, 지원하지 않는 프로젝트는 `needs_input`으로 반환한다.

```sh
uv run python -m scripts.prepare_build --request /worker/request.json \
  --analyzer-command-json '["/opt/iris-analyzer/bin/python","-m","iris_analyzer.build.cli","--request-stdin"]'
```

요청 파일은 다음 snake_case 형식이다. 출력 CLI도 WAS 내부 snake_case이며 외부 분석기와의 camelCase 변환은 Client에서 한 번 수행한다.

```json
{
  "source_directory": "/worker/source",
  "output_directory": "/worker/new-job",
  "source_sha": "0123456789012345678901234567890123456789",
  "root_directory": ".",
  "builder": "dockerfile",
  "dockerfile_path": null,
  "platform": "linux/amd64",
  "allow_generation": true
}
```

## 검증과 의미

- 응답은 source SHA·root·platform·builder와 원본 Dockerfile SHA를 보존해야 한다. 단순히 해시 문자열이 존재하는지만 검사하지 않는다.
- 원본 소스의 포함 파일과 아카이브를 독립 비교한다. 원본 파일 바이트·실행 권한·manifest·근거 SHA가 일치하고, 추가 파일은 생성 Dockerfile 하나여야 한다.
- `source/` prefix 아래 정규화된 tar 경로만 허용하며 경로 이탈·링크·중복·누락·추가를 차단한다. Binary 자산을 보존한다. 명시적 인증 파일, `.env*`, 호스트 의존성과 `secrets/.secrets/credentials`는 제외한다.
- 타임아웃·출력 상한·취소 시 부모 종료 여부와 관계없이 프로세스 그룹을 종료한다. 호스트의 AWS/OpenAI/GitHub 자격증명을 자동 상속하지 않는다.
- `ready`는 준비된 입력을 검증했다는 의미다. 이미지 빌드 성공·ECR push·배포 승인은 후속 단계 결과다. 분석기는 기본 CLI에서 `analysisMode=static`, `dockerfileOrigin=controlled_template`을 명시한다.

실제 두 샘플을 이 CLI에서 분석기에 전달했다. Temp_log는 기존 Dockerfile을 바이트 그대로 재사용했고 portfolio는 Dockerfile 없는 상태에서 생성했다. 반환한 두 아카이브로 Docker 빌드를 통과했고, portfolio의 비특권·read-only 컨테이너에서 `/`, `/healthz`, JS 번들 HTTP 200을 확인했다. Temp_log는 이번에 이미지 빌드와 Node 버전을 확인했으며 DB·전체 앱 E2E를 재수행하지 않았다.

분석기 코드/계약: [iris-code-analyzer-agent 개선 브랜치](https://github.com/2026-softbank-1/iris-code-analyzer-agent/tree/feat/preprocess-deployment-contracts). 평가 상세: [build-preparation-validation](https://github.com/2026-softbank-1/iris-code-analyzer-agent/blob/feat/preprocess-deployment-contracts/reports/build-preparation-validation.md).
