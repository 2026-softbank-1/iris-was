# 0030. 서비스 생성 전 레포 구성 분석(Analysis Gate)은 Build Worker 가 실행하고, 단순 레포는 분석을 생략한다

- 상태: 제안됨 (iris-analyzer wheel 고정·iris-web 연동 대기)
- 날짜: 2026-10-04
- 결정자: 김우현
- 근거: 통합 설계 `DESIGN-analysis-gate.md`(계약 1 분석기 CLI · 계약 2 WAS API · 계약 3 웹)

## 배경
지금은 서비스 1개 = 레포(+root_directory) 1개다. Build Worker 가 `detect_builder` 로 Dockerfile·Railpack 을 고른다. 하지만 compose 로 여러 이미지를 빌드하는 레포나 워크스페이스 모노레포는 서비스 하나로 배포하면 빌드가 실패하거나 일부만 뜬다. 정적 분석기(iris-code-analyzer-agent)는 배포 단위(units)·의존성·환경변수·포트를 뽑을 수 있지만, 모든 레포에 분석 화면을 거치게 하면 단순한 레포의 첫 배포가 느려진다.

제약:
- 신규 k8s 워크로드·인프라 변경 없이 기존 Control API·Build Worker 이미지 안에서 동작해야 한다.
- 소스를 받는 권한(GitHub App 설치 토큰)은 Build Worker 에만 있다(ADR 0001·CLAUDE.md 권한 경계).
- 기존 단일 서비스 경로·오류 진단·코드 수정·배포 흐름은 바뀌면 안 된다.

## 검토한 선택지
1. Control API 가 요청 안에서 분석기를 실행 — 소스 다운로드 권한이 API 에 생기고, 큰 레포는 요청 시간을 넘긴다.
2. 분석 전용 Deployment 추가 — 인프라 변경이 필요하다.
3. `jobs` 큐에 `ANALYZE` kind 추가 — `jobs.deployment_request_id` 가 NOT NULL 이고 선점 쿼리가 배포 요청·서비스와 조인한다. 서비스가 생기기 전의 분석을 넣으려면 큐 계약 전체를 바꿔야 한다.
4. 별도 테이블 `repository_analyses` 를 Build Worker 의 작은 루프가 직접 선점 — 큐 계약을 건드리지 않는다. 깨우기는 기존 `jobs` LISTEN 채널을 재사용한다.

## 결정
4 를 택한다.

| ID | 결정 | 근거 |
|---|---|---|
| D-1 | `repository_analyses`(QUEUED → RUNNING → SUCCEEDED·FAILED, apply 뒤 APPLIED)에 분석 1건을 담는다. 분석기 응답(`iris.analysis-gate.v1`) 원문을 `result`(JSONB)에 그대로 둔다 | 웹이 분석기 계약을 그대로 렌더링한다. WAS 는 쓰는 필드(decision·units·simpleBuild)만 검증한다 |
| D-2 | 접수(`POST …/repository-analyses`, 202) 때 브랜치 최신 커밋을 `source_sha` 로 고정한다. apply 의 배포 요청도 그 커밋으로 만든다 | 분석한 소스와 배포한 소스가 같다 |
| D-3 | Build Worker 가 `FOR UPDATE SKIP LOCKED` 로 QUEUED 또는 lease(`locked_until`)가 만료된 RUNNING 을 선점한다. lease = 분석기 제한 시간 + 5분. 3번 넘게 선점되면 `ANALYSIS_INTERRUPTED` 로 끝낸다 | Worker 가 죽어도 다른 Worker 가 처음부터 다시 실행한다. 분석은 외부 상태를 바꾸지 않아 다시 실행해도 안전하다 |
| D-4 | 분석 접수·반납 트리거가 `NOTIFY jobs, 'REPOSITORY_ANALYSIS'` 를 보낸다. Worker 는 `JobWakeup` 을 하나 더 띄워 이 값만 듣는다 | ADR 0019 의 LISTEN/NOTIFY 를 그대로 쓴다. 놓친 알림·만료 lease 는 60초 기본 주기로 회수한다 |
| D-5 | 소스는 빌드와 같은 GitHub App 설치 토큰·tarball 로 받는다. 일반 파일·디렉터리만 풀고(링크·장치 버림, `tarfile` data 필터로 경로 이탈 차단), 업로드와 같은 풀린 크기·항목 수 한도를 쓴다 | 분석기는 링크를 따라가지 않는다. 압축 폭탄을 막는다 |
| D-6 | 분석기는 `ANALYSIS_GATE_COMMAND`(JSON argv, 기본 `python -m iris_analyzer.gate.cli --request-stdin`)를 셸 없이 새 프로세스 그룹으로 실행한다. 환경변수는 `PATH`·`LANG`·`PYTHONNOUSERSITE` 만 넘기고, 출력은 2MiB·stderr 64KiB 로 자르며, 시간 초과·취소 때 그룹 전체를 SIGKILL 한다. 실패 메시지에 자식 출력을 싣지 않는다 | Worker 의 DB·GitHub·AWS 자격증명이 분석기에 새지 않는다. `feat/ai-dockerfile-preparation` 의 `SubprocessAnalyzerBuildClient` 패턴 |
| D-7 | 분석기는 고정 커밋으로 빌드한 wheel 을 `vendor/` 에 두고(`vendor/iris-analyzer-manifest.json` 에 커밋·sha256) uv path 의존성으로 같은 이미지에 설치한다 | 별도 이미지·배포 없이 Worker 이미지 하나로 끝난다. 재현 가능한 고정 버전이다 |
| D-8 | `decision=skip` 이면 웹이 기존 `POST /projects/{id}/services` 에 `analysisId` 를 붙여 부른다. 서비스 `analysis_plan.gate` 에 결정을 남기고, 분석기의 `simpleBuild`(빌더·Dockerfile 경로)를 기본값으로 쓴다. 분석은 SUCCEEDED 로 남는다 | 단순 레포는 기존 경로와 같다 |
| D-9 | `decision=analyze` 면 `POST …/apply` 가 고른 unit 마다 서비스를 만든다(이름·브랜치·타깃 규칙은 `create_service` 와 같다). 서비스 생성과 APPLIED 전이는 한 트랜잭션이라 일부만 생기지 않는다. 다시 보내면 그때 만든 서비스를 돌려준다(멱등) | 웹 재시도·중복 클릭에 안전하다 |
| D-10 | `deploy=true` 면 서비스마다 기존 수동 배포 요청(`ManualDeploymentService`, `MANUAL`)을 분석 커밋·고정 키(`analysis-{id}`)로 만든다 | 서비스 생성은 원래 배포를 만들지 않는다. 같은 키라 apply 를 다시 보내면 빠진 요청만 생긴다 |
| D-11 | 분석 실패(`FAILED`, errorCode)는 서비스 생성을 막지 않는다. 웹이 "분석 없이 단일 서비스로 생성"(기존 경로, analysisId 없이)을 제공한다 | 분석기 장애가 배포 장애가 되지 않는다 |

## 결과
- 새 API: `POST·GET /projects/{projectId}/repository-analyses[/{analysisId}]`, `POST …/{analysisId}/apply`. 서비스 생성의 선택 필드 `analysisId`, 서비스 응답의 `analysisGate`.
- Build Worker 프로세스에 분석 슬롯(`ANALYSIS_GATE_CONCURRENCY`, 기본 2)이 빌드 슬롯과 따로 생긴다. 분석 중 Worker 메모리·CPU 사용이 는다(정적 분석, 모델 호출 없음).
- 종료 신호(SIGTERM)를 받으면 진행 중 분석기를 죽이고 분석을 QUEUED 로 반납한다(시도 횟수는 되돌린다).
- 업로드 소스(`likelion up`)는 지원하지 않는다. 업로드는 이미 있는 서비스에 묶이는데(`service_uploads.service_id`), 분석은 서비스를 만들기 전이다.
- DB·Redis 같은 이미지 전용 의존성은 플랫폼이 만들지 않는다. 결과의 `dependencies` 로 안내만 하고 사용자가 Variables 로 연결한다.
- 후속: 분석기 `ai:true`(모델 보강)는 범위 밖이다. 오래된 분석 행 정리는 필요해지면 만료 정책을 둔다.
