# 0021. 배포 상세 화면의 로그 API 는 Control API 가 읽기 전용으로 조회하고 빌드 로그는 CloudWatch 에서 읽는다

- 상태: 수락됨 (iris-infra 의 Control API IAM 권한 추가 대기)
- 날짜: 2026-10-03
- 결정자: 김지민

## 배경
배포 상세 화면(`/project/{projectId}/service/{serviceId}/deployment/{deploymentId}/details`)은 탭이 4개다. Details 는 기존 상세 API(`GET .../deployments/{id}`) 응답을 쓰지만, Build Logs·Deploy Logs·Network Logs 는 배포 단위로 읽을 API 가 없어 `Logs aren't available yet` 로 비어 있다.

세 로그의 원본은 서로 다른 곳에 있다.

- **빌드 로그**: CodeBuild 가 CloudWatch Logs 그룹(`/aws/codebuild/{project}`)에 쓴다. ADR 0020 이 AI 진단용으로 Build Worker 가 **실패한 빌드의 끝부분**(최근 200줄·64KB, 비밀 패턴은 가림)을 `builds.log_tail` 에 남기게 했지만, 성공한 빌드와 진행 중인 빌드의 로그, 실패한 빌드의 앞부분은 DB 에 없다.
- **배포(런타임) 로그**: 서비스 단위로는 Loki 가 있다(PR #13). Deploy Worker 가 Pod 에 `release.id` 를 넣고 수집기가 `iris_release_id` 라벨로 올리므로 배포 단위로 거를 수 있다(2026-10-02 결정: Loki + 릴리스 라벨).
- **네트워크 로그**: ALB 접근 로그 → S3 → iris-infra 수집기 → Loki(`job="iris-alb-access"`) 경로가 인프라에 준비돼 있다(`feat/alb-log-to-loki`, 아직 main·dev 에 없음). 수집기는 URL·메서드·IP·User-Agent 를 Loki 로 보내지 않고 상태 코드·바이트·응답 시간만 보낸다. 배포를 구분하는 라벨은 없다.

권한 경계가 걸린다. 흐름 문서 §3 은 Control API 에 CodeBuild 실행·ECR push·GitOps 변경·클러스터 접근을 금지한다. 읽기 전용 Loki·Prometheus 조회와 ADR 0020 의 S3 스냅샷 읽기(`s3:GetObject`)만 예외다. ADR 0020 은 "Control API 에 CloudWatch 권한을 더 주는 대신 Build Worker 가 실패 로그의 끝부분을 남긴다"고 정했는데, 그것은 진단 에이전트에 넘길 끝부분이면 충분해서였다. 화면은 성공한 빌드와 진행 중인 빌드의 로그 전체가 필요하다.

## 검토한 선택지
1. Control API 가 CloudWatch Logs 를 읽기 전용으로 조회한다 — 구현이 단순하고 진행 중인 빌드의 로그도 바로 보인다. 저장소가 늘지 않는다. Control API 에 AWS 읽기 권한이 하나 더 생긴다.
2. Build Worker 가 빌드 중 로그를 DB·S3 로 복사한다 — Control API 에 AWS 권한이 늘지 않는다. 대신 Worker 가 로그를 계속 읽어 써야 하고(행 수·저장 비용·유실 처리), 새 스키마와 마이그레이션이 필요하며, 진행 중 로그가 Worker 폴링 주기만큼 늦는다.
3. ADR 0020 의 `builds.log_tail` 만 보여 준다 — 권한이 늘지 않고 지금 바로 된다. 하지만 실패한 빌드의 끝부분뿐이라 성공한 빌드(화면의 대부분)는 비어 있다.
4. 화면에 CodeBuild 콘솔 링크(`builds.log_url`)만 준다 — 구현이 없다. 요구(탭에 데이터 표시)를 충족하지 못하고 사용자가 AWS 콘솔 권한을 가져야 한다.

## 결정
1 을 택하고, CloudWatch 를 읽도록 설정되지 않은 환경에서는 3 으로 대신한다.

- Control API Role 에는 **빌드 로그 그룹 하나의 `logs:GetLogEvents`** 만 준다. CodeBuild·ECR 호출 권한은 주지 않는다. Loki·Prometheus 조회와 같은 읽기 전용 관측 쿼리로 보고 같은 예외 범주에 넣는다. `CLAUDE.md` 와 흐름 문서 §3 에 이 예외를 적었다.
- 클라이언트는 새로 만들지 않고 Build Worker 가 쓰는 `CloudWatchBuildLogClient`(`app/clients/aws_clients.py`)에 앞에서부터 읽는 `read_events` 를 더했다. Control API 용 `logs` 클라이언트는 S3 서명 설정(`s3v4`)이 없는 기본 설정으로 만든다.
- 설정은 `AWS_REGION`·`BUILD_LOG_GROUP` 이다. 둘 중 하나라도 없으면 저장된 끝부분(`builds.log_tail`)만 돌려주고 `isPartial` 로 알린다. 그것도 없고 읽을 로그가 있으면 `503 NOT_CONFIGURED` 다(다른 API 는 영향이 없다). 그래서 인프라 권한이 적용되기 전에도 실패한 빌드의 끝부분은 보인다.
- 로그 스트림 이름은 `builds.codebuild_build_id`(`{project}:{uuid}`)의 uuid 다. 롤백·재시작은 빌드를 새로 하지 않고 원본 빌드를 복사하므로(ADR 0015) `source_deployment_request_id` 를 따라가 실제로 빌드한 배포의 로그를 돌려주고, 응답의 `loggedDeploymentId` 로 어느 배포 로그인지 알린다. `REMOVE` 요청은 빌드가 없어 빈 결과다.
- 읽기 방식은 `GetLogEvents`(`startFromHead`)와 `nextForwardToken` 이다. 응답의 `nextCursor` 로 이어 읽고, 진행 중인 빌드는 같은 값으로 폴링한다. 검색 파라미터는 두지 않는다(토큰 방식이 달라지고 화면이 읽어 온 줄에서 거를 수 있다).
- CloudWatch 에서 읽은 로그는 비밀 패턴을 가리지 않는다. 서비스 소유자가 자기 빌드 로그를 보는 용도이고, 가리는 일은 ADR 0020 이 외부(에이전트)로 보내거나 DB 에 남길 때 한다.
- 배포 로그는 기존 Loki 클라이언트에 `iris_release_id` 필터를 더해 조회한다. 기본 구간은 배포가 `DEPLOYING` 이 된 때부터 교체될 때까지다. stdout/stderr 구분 라벨은 확인되지 않아 `stream` 필터는 만들지 않는다.
- 네트워크 로그는 같은 Loki 에서 `job="iris-alb-access"` 스트림을 서비스 namespace 로 거른다. 배포 구분 라벨이 없어 **그 배포가 서비스한 구간**(`SUCCEEDED` 가 된 때부터 교체될 때까지)의 시간 범위로 나눈다. 성공하지 못한 배포는 서비스한 적이 없으므로 조회하지 않고 빈 결과를 돌려준다. 항목은 상태 코드·바이트·응답 시간뿐이며 TargetGroup ARN 같은 내부 값은 내보내지 않는다.
- Details 는 기존 응답에 `source`·`configuration`·`build`·`releases`·`replacedBy` 를 더한다(하위 호환). 스키마는 바꾸지 않는다. Root directory·Build command·Port 는 배포 시점 스냅샷이 아니라 서비스의 현재 값이다. 스냅샷은 컬럼 추가가 필요해 이번 범위에서 뺀다.

## 결과
- 세 탭이 쓸 API 가 생기고, Control API 의 AWS 권한은 읽기 전용(스냅샷 `s3:GetObject` + 빌드 로그 `logs:GetLogEvents`)으로 한정된다.
- **iris-infra 에 Control API Role 의 `logs:GetLogEvents`(빌드 로그 그룹 한정)와 `AWS_REGION`·`BUILD_LOG_GROUP` 을 요청해야 한다.** 그 전에는 실패한 빌드의 끝부분(`isPartial`)만 보이고 성공한 빌드는 `503` 이다. Build Worker 의 로그 읽기 권한(ADR 0020)과는 별개다.
- 네트워크 로그는 ALB 수집기가 dev 에 배포되기 전까지 비어 있다. 수집기가 URL·메서드를 보내지 않아 Railway 처럼 `GET /path` 형태는 보여 줄 수 없다. 경로가 필요하면 수집기 정책(개인정보)을 바꾸는 별도 결정이 필요하다.
- ALB 로그는 시간 구간으로 나눠서 배포 경계 부근에서 이전·다음 배포의 요청이 섞일 수 있다.
- `GetLogEvents` 호출에는 계정·리전 단위 한도가 있다. 화면의 폴링 간격을 짧게 잡지 않는다.
- 실제 CodeBuild·Loki·ALB 수집 경로로는 검증하지 못했다. 스트림 이름 규칙(uuid)·`iris_release_id` 라벨은 문서와 코드를 근거로 했고, 배포 후 실제 배포 한 건으로 확인한다([배포 상세 화면 API](../deployment-details-api.md)). 스트림 이름은 CodeBuild 가 주는 `logs.streamName`(Worker 가 `CodeBuildResult.log_stream` 으로 이미 읽는다)을 DB 에 저장하면 가정이 필요 없어진다. 컬럼 추가라 후속으로 미룬다.
- 후속: 진행 중 배포를 위한 Deploy Logs SSE 는 필요해지면 서비스 로그 SSE(`/logs/stream`)에 release 필터를 더하는 방식으로 한다.
