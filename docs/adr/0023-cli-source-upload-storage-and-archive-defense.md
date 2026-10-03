# 0023. `likelion up` 업로드는 Control API 가 받아 S3 에 두고, Build Worker 가 검사하며 스냅샷으로 다시 묶는다

- 상태: 제안됨 (iris-infra IAM·설정 적용, CLI 와의 E2E 대기)
- 날짜: 2026-10-03
- 결정자: 김지민

## 배경
`likelion up` 은 현재 폴더를 tar.gz 로 묶어 올리고 `CLI` 배포 요청으로 배포한다. 서버 계약은 iris-cli 레포의 `docs/up-contract.md` 다(`POST /services/{id}/uploads` 는 본문이 곧 아카이브, `POST /services/{id}/deployments` 의 `triggerType=CLI` + `uploadId`).

지금 빌드는 Build Worker 가 GitHub tarball 을 받아 `snapshots/{buildId}.tar.gz` 로 S3 에 두고 CodeBuild 에 presigned URL 로 넘긴다. 업로드는 그 소스 자리에 끼워야 한다. 확인한 제약:

- **buildspec 은 스냅샷을 `tar -xz --strip-components=1` 로 푼다**(iris-infra `buildspec.yml`). GitHub tarball 은 최상위에 `{owner}-{repo}-{sha}/` 가 하나 있지만, 업로드는 소스의 루트가 곧 아카이브의 루트라서 그대로 두면 첫 경로 요소가 잘려 나간다.
- **Control API Pod 에는 S3 쓰기 권한이 없다.** iris-infra 의 Control API 역할(`iris-dev-control-api`, PR #46 으로 병합·적용됨)은 CloudWatch `logs:GetLogEvents` 만 가지며, Build Worker 는 `snapshots/*` 만 읽고 쓴다. 컴포넌트끼리 IAM Role 을 공유하지 않는다.
- 버킷은 모든 객체를 1일 뒤 지운다(lifecycle `days=1`, S3 는 생성 시각에서 24시간 뒤를 다음 자정(UTC)으로 올림하므로 실제 보존은 24~48시간). SSE-S3, TLS 만 허용.
- 아카이브는 사용자 입력이다. 압축 해제 위치 밖으로 나가는 경로(절대 경로·`..`), 링크를 통한 쓰기, 장치 파일, 압축 폭탄이 빌드 단계에서 문제를 일으키지 않아야 한다.

## 검토한 선택지

저장 방식
1. **Control API 가 본문을 받아 S3 에 직접 저장한다(계약 그대로).** 계약이 한 단계로 단순하고 API 가 크기·형식·소유를 모두 본 뒤에야 저장한다. 하지만 API Pod 가 최대 250MB 를 중계한다(임시 디스크·대역폭·ALB 연결 유지 시간). API Pod 에 S3 쓰기 권한이 생긴다.
2. **presigned PUT: API 가 URL 을 주고 CLI 가 S3 에 직접 올린다.** API Pod 의 디스크·대역폭 부담이 없다. 하지만 계약이 2단계(URL 발급 → 업로드 → 완료 통지)로 바뀌어 CLI 도 바뀌어야 한다. 크기·gzip 검사를 S3 가 하지 못하므로(`content-length-range` 조건은 POST 정책에서만 쓴다) 올린 뒤 다시 검증해야 한다. URL 을 서명하는 API 역할에는 **어차피 `s3:PutObject` 가 필요하다**(IAM 변경은 1 과 같다).
3. **DB large object(또는 `bytea`)에 둔다.** 새 IAM 이 필요 없다. 하지만 최대 250MB 를 RDS 에 쓰면 WAL·백업·연결 점유가 커지고, asyncpg 에는 large object 를 위한 일급 API 가 없어 조각 단위 SQL 함수로 우회해야 한다. Build Worker 는 이를 읽어 다시 S3 스냅샷으로 올려야 한다.

아카이브 방어
- **A. API 에서 tar 내용을 검사한다.** 잘못된 아카이브를 업로드 시점에 422 로 알릴 수 있다. 하지만 최대 250MB 를 API Pod 가 한 번 더 풀어야 하고, 방어선이 사용(소비) 지점이 아니라 앞에 놓인다.
- **B. Build Worker 가 소비할 때 검사하며 다시 묶는다.** 어차피 최상위 디렉터리를 붙이려면 다시 묶어야 하므로 검사를 같은 패스에 넣는다. CPU 를 Worker 가 쓰고, 방어선이 CodeBuild 로 가는 입구에 있다. 단점: 잘못된 아카이브는 업로드가 아니라 빌드 시점에 `FAILED` 로 드러난다.
- **C. CodeBuild 의 GNU tar 에 맡긴다.** GNU tar 는 절대 경로와 `..` 를 걸러 주지만, 링크 사슬·하드 링크·특수 파일·압축 폭탄까지의 방어는 tar 버전과 옵션에 기대야 하고, 막혀도 사용자에게 원인을 알릴 수 없다. 사용자 코드가 도는 빌드 환경 안에서 일어나는 일이라 방어선으로 삼기 어렵다.

## 결정
**저장은 1, 방어는 B 로 한다.** 계약을 바꾸지 않고, CLI 는 한 번의 호출로 올린다. presigned PUT(2)은 API Pod 가 부담이 되면 전환할 후속 선택지로 남긴다(전환하면 계약이 2단계가 되므로 CLI 와 함께 바꾼다).

### 받기(Control API)
`POST /api/v1/services/{serviceId}/uploads` 는 다음 순서로 거절하거나 받는다.

1. 서비스 소유 확인(아니면 `404 SERVICE_NOT_FOUND`).
2. `Content-Length` 필수(없으면 `422`), 0 이하는 `422`, 한도(250MB, `UPLOAD_MAX_BYTES`) 초과는 **본문을 읽기 전에** `413 UPLOAD_TOO_LARGE`.
3. 본문의 첫 두 바이트가 gzip 매직(`1f 8b`)이 아니면 `415 UPLOAD_NOT_GZIP`(첫 조각에서 끊는다).
4. 임시 파일로 스트리밍하며 sha256·크기를 센다(메모리에 올리지 않는다). 받은 크기가 `Content-Length` 와 다르면 `422`.
5. S3 `uploads/{publicId}.tar.gz` 에 올린 뒤 `service_uploads` 행을 만든다(반대 순서면 파일 없는 행이 생길 수 있다. 행 없이 남는 파일은 버킷 lifecycle 이 지운다).

소유 확인 뒤 읽기 트랜잭션을 닫아, 오래 걸리는 업로드 동안 DB 연결을 잡지 않는다. 로컬에서 200MiB 를 올려도 서버 RSS 가 늘지 않음(≈120MB 유지)과, 한도 초과 요청이 본문 없이(`Expect: 100-continue`) 또는 약 1MB 만 받고 `413` 으로 끝남을 확인했다.

### 업로드의 수명
- 공개 ID 는 `secrets.token_urlsafe(32)`(256비트)다. 업로드 응답과 배포 요청만 이 값을 쓴다.
- **만료 24시간, 1회용.** 요청에 묶이면 `consumed_at` 을 채운다. 만료됐거나 쓰였으면 `409 UPLOAD_UNAVAILABLE`, 모르거나 다른 서비스의 것이면 `404 UPLOAD_NOT_FOUND`(둘을 구분해 알리지 않는다).
- **묶기는 원자적이다.** `UPDATE service_uploads SET consumed_at=now WHERE id=? AND consumed_at IS NULL AND expires_at>now RETURNING id` 한 문장이 이긴 쪽을 정한다. 같은 트랜잭션에서 배포 요청을 만들고, 진행 중인 배포가 있어 요청을 만들지 못하면 롤백해 **업로드를 되돌려 준다**(다시 시도할 수 있다). `deployment_requests.service_upload_id` 의 UNIQUE 제약이 마지막 보루다. 8개 동시 요청이 한 업로드를 두고 경쟁해도 하나만 이기는 것을 통합 테스트로 확인한다.
- 같은 `Idempotency-Key` 로 다시 보내면(CLI 의 재시도) 업로드가 이미 쓰였더라도 처음 만든 요청을 돌려준다. 업로드를 가져가기 전에 키를 먼저 찾는다.
- **정리.** DB: 새 업로드를 받을 때 쓰이지 못하고 만료된 지 1일이 지난 행을 지운다(쓰인 행은 배포 요청이 가리켜 남긴다. 행이 작다). S3: 버킷 lifecycle(1일)에 맡긴다. 별도 삭제 권한(`s3:DeleteObject`)을 주지 않는다. TTL 24시간은 이 보존(최소 24시간) 안에서 쓰이도록 정했다.
- 쓰인 뒤에는 아카이브가 곧 사라지므로 **`REDEPLOY`(같은 소스를 다시 빌드)는 거절한다**(`422`). 롤백·재시작은 이미 만든 이미지를 쓰므로 그대로 된다.

### 소스로 쓰기(Build Worker)
`CLI` 요청의 BUILD job 은 GitHub 토큰·tarball 대신 다음을 한다.

1. 업로드 행을 읽고, 압축 크기가 `SNAPSHOT_MAX_BYTES` 를 넘으면 `SOURCE_TOO_LARGE`.
2. S3 에서 내려받는다(없으면 `SOURCE_REF_NOT_FOUND`, 일시적 오류는 재시도).
3. 크기·sha256 이 기록과 같은지 확인한다(다르면 `SOURCE_INVALID`).
4. **검사하며 다시 묶는다**(`app/services/source_archive.py`). 결과를 `snapshots/{buildId}.tar.gz` 에 올린다. 이후 CodeBuild·배포 단계와 AI 진단은 GitHub 요청과 같다.

다시 묶는 규칙(아카이브를 디스크에 풀지 않고 항목을 스트림으로 읽어 새 아카이브에 쓴다):

| 위협 | 방어 |
|---|---|
| 절대 경로, `..` 가 든 경로, 4096바이트가 넘는 경로 | 거절(`SOURCE_INVALID`) |
| 링크를 통한 쓰기(`link -> dir` 다음 `link/evil`) | 파일·링크 아래의 항목은 거절 |
| 루트 밖을 가리키는 심볼릭 링크, 절대 경로 링크 | 거절. **링크 사슬**(`d/s -> ..`, `t -> d/s/..`)은 글자 정리로는 놓치므로 모든 항목을 읽은 뒤 가상 파일시스템에서 실제 경로 해석처럼 따라가 확인한다(링크 40번 이내, 5000개 이내) |
| 하드 링크로 임의 파일에 연결 | 이미 나온 일반 파일을 가리킬 때만 허용하고 대상 경로에 최상위 디렉터리를 붙인다 |
| 장치·FIFO 등 특수 파일, 중복 경로 | 거절 |
| setuid·소유자·xattr(file capabilities) | uid·gid·이름을 비우고 mode 를 `& 0o777` 로 줄이며 확장 헤더는 옮기지 않는다 |
| 압축 폭탄 | 풀었을 때 총 크기(2GiB)·항목 수(10만) 한도(`SOURCE_TOO_LARGE`). 읽는 바이트에 예산을 걸어, 크기를 속인 헤더·거대한 확장 헤더(항목당 1MiB 상한)·항목마다 반복되는 확장 헤더가 메모리·시간을 키우기 전에 끊는다 |

CLI 는 폴더 밖을 가리키는 링크를 빼고 안쪽을 가리키는 `..` 링크(`src/config.json -> ../shared/config.json`)는 올린다. 서버가 `..` 를 통째로 막으면 정상 업로드를 거절하게 되므로, 루트 안에 머무는 링크는 허용한다.

실제 CLI(node-tar)가 만든 아카이브(긴 경로·한글 이름·실행 비트·심볼릭·하드 링크)를 다시 묶어 CodeBuild 와 같은 GNU tar 1.34 로 `--strip-components=1` 풀어 모두 정상임을 확인했다.

검사하지 않는 것: 아카이브 **내용**이 악의적인지(소스 코드이므로 GitHub 소스와 같다 — CodeBuild 의 격리와 권한 경계가 맡는다).

### `services.root_directory`
**업로드는 서비스 소스의 루트로 본다.** `root_directory`(저장소 안의 위치)는 업로드에 적용하지 않는다. `likelion up` 은 연결한 폴더, 곧 서비스 폴더를 올리기 때문이다. 빌드 환경변수 `ROOT_DIRECTORY` 는 `.`, `iris.json`·`Dockerfile` 위치는 아카이브 루트 기준이다(`dockerfile_path` 는 서비스 설정 그대로 루트 기준 상대 경로). 진단 에이전트에도 `rootDirectory: "."` 를 넘긴다.

### `source_sha`
`upload-` + 아카이브 sha256 의 앞 12자(예: `upload-3fa9c2d1b7e4`, 19자). Git SHA 는 16진수뿐이라 이 값과 겹칠 수 없어, 값만 보고 GitHub 소스인지 가릴 수 있다(재배포 거절, 진단의 커밋 SHA 생략). 같은 내용을 올리면 같은 값이다. `IRIS_GIT_COMMIT_SHA` 환경변수에도 이 값이 들어간다.

### 실패 코드
`SOURCE_INVALID`(손상·허용하지 않는 항목·체크섬 불일치)를 더한다. 기존 `SOURCE_TOO_LARGE`·`SOURCE_REF_NOT_FOUND` 를 재사용한다. 사용자에게는 코드만 보이고 어떤 항목 때문인지는 로그(`fields.path`)에만 남는다 — 업로드 시점 검사(A)를 도입하면 메시지를 줄 수 있다.

## 인프라 변경 (iris-infra, 이 저장소에서는 적용하지 않는다)
이 기능은 아래 없이는 운영에서 동작하지 않는다. Control API 역할(`iris-dev-control-api`)은 iris-infra PR #46 으로 이미 병합·적용돼 있고, 지금 권한은 CloudWatch `logs:GetLogEvents` 뿐이다. 이 역할에 새 인라인 정책으로 권한을 더한다. 기존 `read-build-logs` 정책은 그대로 둔다.

```hcl
# foundation/control-api-identity.tf — 기존 aws_iam_role_policy.control_api(read-build-logs)는 두고 새 인라인 정책을 더한다
resource "aws_iam_role_policy" "control_api_source_uploads" {
  name = "source-uploads"
  role = aws_iam_role.control_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteSourceUploads"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.build_artifacts.arn}/uploads/*"
      },
      {
        # 진단이 소스를 함께 보낼 때(ARTIFACT_BUCKET 이 설정되면 켜진다). ADR 0020
        Sid      = "ReadSnapshotsForDiagnosis"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.build_artifacts.arn}/snapshots/*"
      }
    ]
  })
}
```

```hcl
# foundation/build.tf — aws_iam_role_policy.build_worker 의 Statement 에 추가
{
  Sid      = "ReadSourceUploads"
  Effect   = "Allow"
  Action   = ["s3:GetObject"]
  Resource = "${aws_s3_bucket.build_artifacts.arn}/uploads/*"
}
```

- 버킷 lifecycle·암호화·TLS 정책은 바꾸지 않는다. 키가 없을 때 Worker 가 `403` 대신 `404` 를 받으려면 `uploads/` 접두어 한정 `s3:ListBucket` 을 줄 수 있다(없어도 동작하며 `403` 은 재시도 후 실패한다).

### 운영 절차
순서를 지킨다. 2 의 환경변수가 업로드 API 와 진단의 소스 전송을 **함께** 켜므로, 1 의 Role 권한(Control API 의 `uploads/*` Put·AbortMultipartUpload, `snapshots/*` Get)이 먼저 적용돼 있어야 한다. 권한 없이 켜면 진단이 받는 presigned URL 이 `403` 을 준다.

1. **iris-infra 정책 PR 을 병합한다**(CI 가 apply. 2026-10-03 iris-infra PR #50 이 병합·apply 됐고 AWS 에서 두 역할의 정책을 확인했다). 위 Control API 인라인 정책과 Build Worker 의 `ReadSourceUploads` 를 함께 올린다. 역할이 이미 Pod 에 연결돼 있고 IAM 인라인 정책 변경은 떠 있는 Pod 에도 바로 적용되므로 **재시작이 필요 없다**. Pod 재시작은 새 Pod Identity association 을 만들 때만 필요하다.
2. **운영 Secret `iris-platform-was-env` 에 `ARTIFACT_BUCKET` 키를 추가한다.** dev 값은 `iris-dev-build-artifacts-187069338876-ap-northeast-2` 다. chart 값(`api.artifactBucket` 같은 것)으로 넣지 않는다. Argo CD root 가 iris-infra 의 특정 SHA 에 고정돼 있어 iris-infra 의 chart ConfigMap 변경이 클러스터에 바로 들어가지 않기 때문이다. Build Worker 는 이미 ConfigMap 으로 `AWS_REGION`·`ARTIFACT_BUCKET` 을 갖고 있어 바꾸지 않는다. API 의 `AWS_REGION` 은 ConfigMap `iris-platform-api` 와 Pod Identity 웹훅이 이미 넣어 주므로(2026-10-03 운영 Pod 에서 확인) Secret 에는 `ARTIFACT_BUCKET` 만 넣는다.
3. **API 를 롤링 재시작한다.** 환경변수를 바꾼 API 에만 필요하다. Build Worker 는 환경변수도 새 association 도 없으므로 재시작하지 않는다.
4. **확인한다.** 로그인한 사용자의 토큰으로 본인 서비스에 빈 본문 `POST /api/v1/services/{id}/uploads` 를 보내 `503 NOT_CONFIGURED` 가 `422 INVALID_INPUT`(`Content-Length` 0)으로 바뀐 것을 본다. 이어서 같은 사용자로 `likelion up` 을 끝까지 돌려 배포가 `SUCCEEDED` 로 끝나는지 본다.

## 결과
- 계약 변경 없이 `likelion up` 이 한 번의 업로드와 한 번의 배포 요청으로 끝난다. GitHub 경로는 바뀌지 않는다.
- Control API 의 AWS 권한이 읽기 전용 CloudWatch(PR #46)에서 `uploads/*` 쓰기까지 넓어진다. 쓰기는 접두어와 두 Action 으로 제한한다.
- 다시 묶는 시간은 실측으로 압축이 잘 되는 소스에서 약 500MiB/s, 압축이 안 되는 바이너리에서 약 50MiB/s(개발 머신 1코어)다. Build Worker 는 스냅샷 구간에서 job lease(5분)를 갱신하지 않는데(`job_repository` 의 ponytail 주석), 한도(압축 250MB·풀었을 때 2GiB)를 다 채워도 이 안에 끝난다고 본다. 넘기는 일이 생기면 heartbeat 를 둔다.
- API Pod 가 업로드마다 최대 250MB 임시 파일을 쓴다. 동시 업로드가 늘면 Pod 의 임시 디스크가 문제가 될 수 있다. **후속**: 동시 업로드 수 제한, presigned PUT 전환(계약 2단계), 업로드 시점 검사(`source_archive` 를 API 에서도 호출해 422 로 앞당김).
- 잘못된 아카이브는 업로드가 아니라 빌드 시점에 `FAILED`(`SOURCE_INVALID`)로 드러난다. CLI 가 만든 아카이브는 통과하므로 직접 만든 아카이브에서만 보인다.
- 서비스는 지금 GitHub 저장소(`source_repository_url`·`github_installation_id`)가 필수라, 저장소 없는 서비스에 `up` 하는 흐름은 범위 밖이다.
- 쓰인 업로드는 24~48시간 뒤 S3 에서 사라져 같은 소스의 재빌드는 할 수 없다. 이미지는 롤백·재시작으로 다시 쓴다.
