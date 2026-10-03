# TODO — 런타임 환경변수 (Sealed Secrets)

> **2026-10-03 구현 상태**: 아래 설계는 Helm 전환에 맞춰 구현됐다. 남은 것은 클러스터 쪽뿐이다.
> - 완료: 변수 저장·스냅샷 API(ADR 0017), iris-infra `iris-service` 0.6.0 과 Sealed Secrets addon(코드·tag), Deploy Worker 봉인(`app/clients/secret_sealer.py`, `DeployService._seal_variables`)
> - 남음: `make bootstrap` 으로 클러스터에 반영 → controller 키 백업 → 공개 인증서를 Deploy Worker 설정(`SEALED_SECRETS_CERT`, iris-platform chart 값)으로 전달 → 변수가 있는 서비스로 end-to-end 확인(롤백·빈 값 포함)
> - 이 문서의 `SecretSealer.seal`·`kubeseal` subprocess 안은 `cryptography` 직접 봉인으로 대체됐다. `vars-v{version}` 대신 release 마다 `vars-r{release_id}` 이름을 쓴다.

> [Deploy Worker 계획](deploy-worker-plan.md) MVP 에서 뺀 기능. 원본 계획의 설계를 보존한다.
> 이 기능 위에 [pre-deploy](todo-pre-deploy.md)·[PORT 지정](todo-custom-port.md) 이 올라간다.

> ⚠️ **2026-10-02 Helm 전환 영향**: 아래 "서비스 디렉터리에 `vars-v*.json`(SealedSecret manifest)을 커밋" 설계는 더 이상 동작하지 않는다. Argo CD 는 `iris-service` chart 만 렌더링하고 values 디렉터리의 다른 파일은 배포하지 않는다. 재설계 방향: Worker 는 봉인 결과(`encryptedData`)를 `values.yaml` 의 필드(예: `variables.name`·`variables.encryptedData`, 이전 버전 포함)로 넣고, iris-infra chart 가 SealedSecret 과 `envFrom` 을 만든다. chart schema·`contracts/deployment.md` 를 함께 바꾼다. 봉인·재사용·이전 버전 유지·키 관리 원칙은 그대로다.

**목표**: 사용자가 등록한 변수(`DATABASE_URL`, API 키 등)를 앱 컨테이너 환경변수로 넘긴다. 이미지와 변수를 한 커밋에 묶어 revert 한 번으로 함께 되돌린다.

## 선행 조건

- ~~Build 계획 Task 7: `VariableService`, `service_variable_versions`, `builds.variables_version`~~ — **구현됨(2026-10-02, [ADR 0017](../../docs/adr/0017-service-variables-encrypted-storage-and-deploy-snapshot.md))**. 버전 테이블 대신 `service_variables`(키별 Fernet 암호문)와 `deployment_requests.variables_snapshot`(`{key: 암호문}`)을 쓴다. 이 문서의 `variables_version` 은 "snapshot 의 내용 해시" 같은 값이 필요해지면 그때 정한다. 아래 설계의 `VariableService.load(service_id, version)` 은 "요청의 스냅샷을 복호화한다"로 바뀐다.
- Prod 에 Sealed Secrets controller 설치, 키를 Secrets Manager 에 백업(관리자만 읽기) — 코드는 iris-infra PR #43 으로 merge 됐고 bootstrap 이 남았다
- ~~chart 가 사용자 변수를 받는 값을 추가~~ — iris-infra `iris-service` 0.6.0 으로 완료
- Deploy Worker 가 스냅샷을 복호화할 키를 받는 방식 — 지금은 공유 `.env` Secret 으로 받는다. 키를 나누는 것은 후속(ADR 0017)

## 설계

- **봉인**: `VariableService.load(service_id, version)` 으로 복호화하고, 평문을 stdin 으로 `kubeseal --cert <SEALED_SECRETS_CERT> --scope strict -o json` 에 넘긴다. 평문은 메모리에만 둔다. Git 에 평문 변수를 올리지 않는다.
- **재사용**: 같은 버전 파일이 HEAD 에 이미 있으면 다시 봉인하지 않고 복사한다. 봉인할 때마다 결과가 달라져 diff 가 생기기 때문이다.
- **이전 버전 유지**: lastKnownGood 의 변수 파일도 그 release 커밋에서 복사해 함께 둔다(최대 2개). 현재 버전만 두면 prune 이 이전 Secret 을 지워, rollback 을 기다리는 동안 A Pod 가 재시작될 때 `CreateContainerConfigError` 가 난다.
- **리소스**: `SealedSecret` `vars-v{version}`, scope strict, `sync-wave: "-2"`(앱은 0). Deployment 는 `envFrom: [{ secretRef: { name: vars-v{version} } }]`. `env` 가 `envFrom` 보다 우선하므로 `IRIS_*`·`PORT` 를 사용자가 덮어쓸 수 없다.
- **키 관리**: controller 는 30일마다 새 키를 만들지만 이전 키를 지우지 않는다. 키 교체는 운영자가 `SEALED_SECRETS_CERT` 를 갱신하는 것으로 한다. 키를 잃으면 서비스를 다시 배포해 새 키로 봉인한다.

디렉터리 예 (서비스 12, 변수 v3, lastKnownGood 변수 v2):

```text
services/12/prod/
  values.yaml
  vars-v3.json   vars-v2.json
```

## 변경 지점

| 위치 | 변경 |
|---|---|
| `releases` | `variables_version` 컬럼 |
| `DeployWorkerSettings` | `SEALED_SECRETS_CERT` |
| 공용 Dockerfile runner 단계 | `kubeseal` 바이너리(버전 고정). API·Build Worker 도 같은 이미지다 |
| `app/clients/secret_sealer.py` | `SecretSealer.seal(namespace: str, name: str, data: dict[str, str]) -> str` (kubeseal subprocess) |
| 렌더러 | `ReleaseSpec` 에 `variables_secret_name: str \| None`, `previous_sealed_secret: tuple[str, str] \| None` |
| Deploy Worker IAM | 변수 KMS Decrypt (encryption context `service_id`) |
| AppProject whitelist · Prod ClusterRole | `SealedSecret` 추가. `Secret` 은 계속 넣지 않는다 |
| 실패 코드 | 봉인 오류(KMS·kubeseal)는 커밋 전 외부 오류 → `DEPLOY_INFRA_ERROR` |

## 테스트

- 이전 SealedSecret 복사, 같은 버전이면 하나만
- `IRIS_*` 를 덮어쓸 수 없음
- 가짜 인증서로 kubeseal 왕복

## Task 0 확인

- SealedSecret 기본 health check 가 Argo 에 있는지. 없으면 Lua 로 추가한다

---

## 제안 (원본 계획에 없음)

- 파일 복사는 내용을 읽지 않고 **blob SHA 로** 한다. `create_tree` 항목에 `sha` 를 넣으면 되므로 `read_file` 대신 `services/{id}` 의 `{파일명: blob_sha}` 목록 조회 하나면 된다.
- kubeseal 호출은 `asyncio.create_subprocess_exec` 로 한다(이벤트 루프 블로킹 방지).
- 실사용자 대상 MVP 라면 Deploy 계획 Task 7(ROLLBACK) 바로 다음 순서로 넣는다. 변수 없이 돌 수 있는 앱은 정적 사이트·데모 수준이다.
