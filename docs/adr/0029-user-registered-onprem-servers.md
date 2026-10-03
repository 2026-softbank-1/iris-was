# 0029. 사용자가 설치 명령 한 줄로 자기 서버를 배포 타깃으로 붙인다

- 상태: 제안됨 (iris-infra 1회 변경·설치 스크립트 E2E 대기)
- 날짜: 2026-10-04
- 결정자: 김지민
- 계약: [온프레미스 서버 등록 계약](../onprem-server-registration-contract.md) (iris-was·iris-infra·iris-cli·iris-web 공통)

## 배경
온프레미스 타깃은 운영자가 손으로 붙인 서버 1대(타깃 `onprem`, `iris-onprem-01`)뿐이다. 클러스터 접속 정보는 iris-infra 에 손으로 넣었고, 게이트웨이 upstream 도 그 서버 하나로 고정돼 있다([ADR 0027](0027-single-deploy-target-per-service.md)). 사용자가 자기 Ubuntu 서버를 배포 대상으로 직접 붙이려면, 서버 등록·클러스터 연결·이름 규칙·이미지 pull 권한·서비스 변수 봉인을 사용자마다 자동으로 해야 한다.

지켜야 할 경계는 그대로다. Control API 는 GitHub·Argo CD 를 부르지 않고(그 쓰기는 Deploy Worker 몫이다), 컴포넌트끼리 자격증명을 공유하지 않으며, 클러스터 토큰 같은 비밀은 Git 에 평문으로 남기지 않는다.

## 검토한 선택지

등록 방식
1. **운영자 수동 등록.** 지금 방식이다. 사용자가 서버 정보를 보내면 운영자가 iris-infra 에 cluster Secret·Tailscale egress·게이트웨이 upstream 을 추가한다. 코드가 거의 필요 없지만 서버마다 운영자 작업·PR·배포가 들고, 사용자가 기다려야 한다.
2. **Control API 가 Tailscale·클러스터를 직접 부른다.** 등록 API 가 Tailscale API 로 기기를 승인하고, 서버의 K3s API 로 SA 를 만들고, Argo CD 에 cluster 를 등록한다. 즉시 끝나지만 Control API 가 tailnet·사용자 클러스터·Argo CD 쓰기 권한을 모두 갖게 돼 권한 경계가 무너진다. 사용자 서버가 느리거나 꺼져 있으면 요청이 그만큼 묶인다.
3. **서버가 스스로 설정하고(설치 스크립트) 결과만 보고하며, 클러스터 연결은 GitOps 로 한다.** 서버가 등록 토큰으로 설정값을 받아(bootstrap) tailnet 가입·K3s·SA 토큰을 만들고 접속 정보를 보낸다(connect). Deploy Worker 가 그 정보를 봉인해 GitOps 에 커밋하고, management 의 ApplicationSet 이 서버별 cluster Secret·Tailscale egress·probe Application 을 만든다. 운영자 개입이 없고 Control API 권한은 늘지 않는다. 대신 연결 확인이 비동기라 상태(PENDING → REGISTERING → CONNECTED/FAILED)를 둬야 한다.

서비스 생성과의 관계
- **A. 등록과 서비스 생성을 한 번에 한다.** 화면 한 번으로 끝나지만, 연결 확인(최대 15분)이 끝나기 전에 서비스·첫 배포가 생겨 실패한 배포가 쌓인다. 서버 하나를 여러 서비스가 쓰는 경우도 표현하기 어렵다.
- **B. 서버 등록은 따로 하고, 서버마다 전용 타깃을 만들어 기존 타깃 선택(`targetIds`)으로 고른다.** 기존 서비스·배포 흐름을 그대로 쓴다. 연결되지 않은 서버로는 배포 요청을 막는다(409 `TARGET_NOT_CONNECTED`).

Tailscale 가입 키
- **운영자가 발급한 키(reusable·pre-approved·`tag:iris-onprem`)를 설정으로 둔다.** 구현이 단순하다. 키가 새면 누구나 같은 태그로 tailnet 에 들어올 수 있어, 태그를 목적지로만 쓰는 ACL(서버끼리·서버→tailnet 출발 금지)로 피해를 줄이고 키를 주기적으로 바꾼다.
- **Worker 가 Tailscale API 로 서버마다 1회용 키를 발급한다.** 키가 새도 한 번만 쓰인다. 대신 Tailscale OAuth 자격증명과 발급 대기(폴링) 흐름이 필요하다.

Deploy Worker 의 서버 작업 방식(`jobs.deployment_request_id` 가 NOT NULL 이다)
- **a. jobs 큐에 서버 job 종류를 더한다.** 기존 선점·재시도·LISTEN/NOTIFY 를 재사용하지만, `deployment_request_id` 를 nullable 로 바꾸거나 가짜 배포 요청을 만들어야 한다. 앞쪽은 jobs 를 읽는 모든 쿼리(사용자별 빌드 제한의 조인 등)와 배포 요청 cascade 가 흔들리고, 뒤쪽은 배포 이력에 가짜 요청이 남는다.
- **b. 별도 `onprem_server_jobs` 테이블.** 깔끔하지만 큐 하나를 새로 만드는 비용(선점·lease·재시도·정리)이 서버 1대에 할 일 하나(반영·확인·삭제)에 비해 크다.
- **c. `onprem_servers` 행 자체를 lease 로 선점한다.** 서버마다 할 일은 늘 하나이고 상태가 행에 있다. `next_check_at`(할 일 시각, 없으면 할 일 없음)·`locked_by`·`locked_until` 을 두고 `FOR UPDATE SKIP LOCKED` 로 한 Worker 만 가져간다.

## 결정
등록은 3, 서비스와의 관계는 B, Tailscale 키는 운영자 발급 키(나중에 1회용 키로 바꾼다), Worker 는 c 로 한다.

- **데이터**: `onprem_servers` 에 소유자·이름(소유자 안에서 유일, 삭제 제외)·`server_key`(`[a-z][a-z0-9]{7}`)·전용 `target_id`·상태·`failure_code`·토큰/비밀 해시·접속 정보를 둔다. 타깃에 `owner_id` 를 더해 공용 타깃(`owner_id` 없음)과 서버 타깃을 가른다. 타깃도 서버와 함께 소프트 삭제한다. 이름 규칙(타깃 `onprem-{key}`·Tailscale `iris-{key}`·host `{label}-{key}.internal.likelion.uk`)은 모두 `server_key` 에서 나온다.
- **비밀**: 등록 토큰(24시간)과 서버 비밀은 [ADR 0018](0018-cli-login-session-table-and-polling.md) 처럼 `secrets.token_urlsafe(32)` 이고 SHA-256(hex)만 저장하며 응답에서 한 번만 보인다. 요청에는 공개 ID 가 없어 해시로 행을 찾고(unique index), 찾은 뒤 `hmac.compare_digest` 로 다시 비교한다. 256비트 무작위 값이라 해시 조회의 시간 차이로 얻을 것이 없다. bootstrap·connect 는 `PENDING`·`REGISTERING`·`FAILED` 에서 받는다(connect 뒤 스크립트가 중간에 실패해도 같은 명령으로 다시 돌릴 수 있게). bootstrap 은 상태를 바꾸지 않고, connect 는 `FAILED` 도 `REGISTERING` 으로 되돌린다. 실패는 없음·만료·이미 연결됨을 가리지 않고 401 `INVALID_REGISTRATION_TOKEN` 이다. SA 토큰은 서비스 변수와 같은 Fernet 키로 암호화한다.
- **GitOps**: Deploy Worker 가 `platform/onprem-servers/{key}/values.yaml` 을 커밋한다. Argo cluster Secret 의 `config`(SA 토큰·CA·serverName)는 management controller 인증서(`PLATFORM_SEALED_SECRETS_CERT`)로 `argocd/cluster-onprem-{key}` strict scope 로 봉인한다. 삭제는 디렉터리 삭제 커밋이다. 커밋 절차(SHA 를 먼저 기록 → fast-forward, 브랜치가 움직이면 새 HEAD 위에 다시)는 배포와 같은 코드(`GitOpsWriter`)를 쓴다.
- **연결 확인**: 커밋이 main 에 반영된 뒤 15분 안에 probe Application `iris-onprem-probe-{key}` 가 Synced+Healthy 면 CONNECTED, 아니면 FAILED(`CONNECT_TIMED_OUT`). 커밋을 5번 실패하면 FAILED(`GITOPS_COMMIT_FAILED`). Argo CD 는 읽기 전용 Client 로만 본다. probe Application 은 project `iris-onprem-probe` 에 있어 서비스용 `ARGOCD_TOKEN` 으로는 보이지 않으므로 그 project 의 읽기 토큰 `ARGOCD_PROBE_TOKEN` 을 따로 받는다. 없으면 연결 확인을 하지 않고 `REGISTERING` 으로 두며, 기한도 그만큼 미뤄 토큰이 생긴 뒤 15분을 온전히 준다.
- **lease 와 세대**: 선점 트랜잭션은 lease 만 잡고 끝난다. GitHub·Argo CD 호출은 트랜잭션 밖에서 하고, 결과를 쓸 때마다 행을 잠가 선점할 때의 `connect_generation`·삭제 여부와 같은지 본다. 그 사이 connect 를 다시 받았거나(재실행) 삭제됐으면 결과를 버리고 lease 만 놓는다. 이미 만든 커밋은 main 에 올리지 않고(기록 전에 멈춘다) 다음 차례가 새 값으로 다시 만든다. lease(5분)는 커밋 SHA·기한을 기록할 때마다 갱신하고, 기록할 때 lease 가 자기 것이 아니면(만료돼 다른 Worker 가 가져갔거나 토큰 재발급으로 비워졌으면) 결과를 버린다. Worker 가 죽으면 lease 가 끝난 뒤 다른 Worker 가 가져가고, 기록된 커밋이 main 에 있으면 다시 만들지 않는다. 봉인·복호화처럼 GitHub 밖의 실패는 커밋 재시도 횟수를 쓰지 않고 `last_error` 를 남긴 채 30초 뒤 다시 한다.
- **Worker 루프**: Deploy Worker 는 job 하나·서버 하나를 번갈아 처리한다. 깨어나는 시각은 다음 job 의 `run_after` 와 다음 서버의 `next_check_at` 중 이른 쪽이다. `PLATFORM_SEALED_SECRETS_CERT` 와 `VARIABLES_ENCRYPTION_KEY`(SA 토큰 복호화) 가 모두 있어야 서버 동기화를 켠다.
- **배포**: 서버 타깃의 서비스 host 는 `build_service_host` 한 곳에서 `{이름}-{id}-{key}` 로 계산한다(63자를 넘으면 이름을 줄인다). 게이트웨이는 마지막 `-` 뒤가 영문으로 시작하는 8자면 그 서버로 보낸다. 기존 host 는 끝이 `-숫자` 라 겹치지 않는다. 서버로 가는 서비스 변수는 그 서버가 보낸 Sealed Secrets 인증서로 봉인한다. 서버 타깃 values 에는 `imagePullSecrets: [{name: iris-ecr-pull}]` 를 더한다(iris-service chart 0.8.0). 서버의 default SA 패치는 그 뒤에 만든 Pod 에만 먹고, Argo 가 namespace 와 Rollout 을 같이 만들어 첫 Pod 가 먼저 뜰 수 있어서다.
- **이미지 pull**: 서버의 CronJob 이 서버 비밀로 `registry-credentials` 를 부르면, Control API 가 ECR pull 전용 Role 을 AssumeRole 하며 세션 정책으로 그 서버 타깃에 붙은 서비스의 저장소(`iris/services/{id}`)만 허용하고, 그 임시 자격증명으로 ECR 토큰을 받아 준다. 서버 쪽에는 AWS 자격증명이 남지 않는다. 비밀이 틀리면 401, 아직 `CONNECTED` 가 아니면 409 `ONPREM_SERVER_NOT_CONNECTED` 다(CronJob 이 조용히 다음 회차를 기다린다).

## 결과
- 사용자는 등록 → 명령 실행 → CONNECTED 확인 → 서비스 생성만 한다. 운영자 작업은 iris-infra 1회 변경(management Sealed Secrets·ApplicationSet·게이트웨이 정규식·IAM)뿐이다.
- Control API 의 권한은 STS AssumeRole(ECR pull Role) 하나만 는다. GitHub·Argo CD 쓰기는 여전히 Deploy Worker 만 한다.
- 연결 확인이 비동기라 화면·CLI 는 `GET /onprem-servers/{id}` 를 폴링해야 한다. connect 직후 Worker 를 깨우는 알림은 없어서, Worker 가 쉬고 있으면 반영 시작이 최대 1분(대기 상한) 늦을 수 있다.
- probe Application 의 소스는 GitOps 저장소가 아니라 연결 확인에 커밋 revision 을 대조하지 않는다. 재실행으로 접속 정보를 바꾼 직후에는 이전 값으로 된 Healthy 를 볼 여지가 있지만, 이전 값이 Healthy 였다면 이미 CONNECTED 였으므로 실제로는 실패 뒤 재실행에서만 생긴다.
- 세션 정책은 압축해 2048자까지라 서버 하나에 서비스가 25개쯤을 넘으면 자격증명 발급이 502 가 된다(서버의 새 서비스 이미지를 받지 못한다). 실패하면 `onprem_server_id`·`service_count` 를 담은 구조화 오류 로그가 남아 이 한도인지 가릴 수 있다. 서버당 서비스가 그만큼 늘면 저장소 이름을 서버별 접두사로 나눠 와일드카드 ARN 하나로 바꾼다.
- Control API 자격증명이 이미 role 세션이라 AssumeRole 이 연쇄되어 세션은 1시간까지다(`ONPREM_ECR_PULL_SESSION_SECONDS` 기본 3600). ECR 토큰도 그 안에서 끝날 수 있지만 서버 CronJob 이 5분마다 갱신한다. `expiresAt` 은 ECR 토큰 만료와 임시 자격증명 만료 중 이른 쪽이다.
- 서버 디렉터리 삭제 커밋을 5번 실패하면 Worker 가 멈추고 `last_error` 를 남긴다. 운영자가 디렉터리를 지운다.
- 토큰 재발급은 `REGISTERING` 에서도 된다. 잘못된 서버에서 실행했거나 연결이 멈췄을 때 처음부터 하도록, 세대를 올리고 lease 를 비워 Worker 가 하던 일을 버린다. 이미 main 에 올라간 이전 값은 다음 connect 의 커밋이 덮는다.
- 운영자 Tailscale 가입 키 하나를 모든 사용자가 쓰므로, 키가 새면 누구나 `tag:iris-onprem` 으로 tailnet 에 들어올 수 있다. 태그를 목적지로만 쓰는 ACL 로 피해를 줄이고, 사용자마다 서버를 5대(`ONPREM_SERVER_LIMIT_EXCEEDED`)로 묶어 키 노출 횟수를 줄인다. 1회용 키 자동 발급(계약 §10)으로 없앤다.
- 서버에 복사하는 ClusterRole 은 iris-infra 의 배포 권한과 같아 Secret 을 포함한 클러스터 전체 읽기를 준다. 그래서 그 SA 토큰을 쓰는 management Argo CD 는 서버의 `iris-system/iris-server-secret` 도 읽을 수 있다. 서버 비밀이 할 수 있는 일은 그 서버 서비스의 ECR pull 뿐이라 받아들이고, 읽기 범위를 좁히는 것은 iris-infra 규칙과 함께 바꾼다.
- 설치 명령은 등록 토큰을 `--token` 인자로 받아, 실행하는 동안 같은 서버의 다른 사용자가 `ps` 로 볼 수 있다. 토큰은 24시간·1회 등록용이고 connect 뒤에는 재발급으로 무효화할 수 있다.
- `installCommand` 의 주소는 요청의 Host 가 아니라 `API_BASE_URL` 설정에서만 만든다(Host 헤더를 바꾼 요청이 다른 서버를 가리키는 명령을 받지 않게). https 가 아니면(로컬 개발 제외) 등록을 503 으로 막는다.
- 후속: Tailscale 1회용 키 자동 발급, `CONNECTED` 이후 연결 끊김 감지, 서버 쪽 로그·메트릭 수집, 기존 `onprem` 서버를 새 경로로 옮기기(계약 §9·§10).
