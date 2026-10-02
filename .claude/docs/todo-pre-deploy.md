# TODO — pre-deploy command

> [Deploy Worker 계획](deploy-worker-plan.md) MVP 에서 뺀 기능. 원본 계획의 설계를 보존한다.

> ⚠️ **2026-10-02 Helm 전환 영향**: 아래 "`pre-deploy.json` Job 을 렌더링·커밋" 설계는 더 이상 동작하지 않는다(Argo CD 는 chart 만 렌더링한다). 재설계 방향: values 에 `preDeploy.command`·`preDeploy.timeoutSeconds` 를 두고 iris-infra chart 가 hook Job 을 만든다. **rollback 주의**: 지금 rollback 은 이전 release 의 values 디렉터리를 그대로 복원하므로 이전 버전의 `preDeploy` 가 다시 실행된다. revert 커밋에서 `preDeploy` 를 뺀 values 를 새로 써야 한다(디렉터리 복원 → values 재작성으로 바뀜). hook 설정·실패 판정·사용자 요구 사항은 그대로다.

**목표**: 새 버전 앱을 띄우기 직전에 사용자 명령(주로 DB 마이그레이션)을 한 번 실행한다. 실패하면 배포를 멈추고 기존 버전을 유지한다.

## 선행 조건

- [런타임 환경변수](todo-runtime-variables.md). DB 접속 정보 없이는 마이그레이션을 돌릴 수 없다

## 설계

- **입력**: `builds.deploy_config.preDeployCommand` (Build Worker 가 이미 기록한다). 지금은 `deploy` 검증 모델이 거절하므로 필드를 추가한다.
- **Job `pre-deploy`** (`pre-deploy.json`, 명령이 있을 때만)
  - `argocd.argoproj.io/hook: Sync`, `sync-wave: "-1"`(SealedSecret 은 -2, 앱은 0)
  - `backoffLimit: 0`, `activeDeadlineSeconds: 600`, `restartPolicy: Never`
  - 이미지·변수·리소스는 앱과 같다. 명령은 `["/bin/sh","-c", cmd]` 로 실행한다.
- **PreSync 를 쓰지 않는 이유**: PreSync 는 이번 버전의 SealedSecret 보다 먼저 돌아서 변수를 읽지 못한다.
- **실패**: `PRE_DEPLOY_FAILED`. `syncPolicy.retry` 를 두지 않으므로 재시도하지 않는다. sync 가 멈춰 Deployment 는 바뀌지 않는다.
- **pre-deploy 성공 후 앱 실패**: 자동 rollback 한다. DB 변경은 남는다. 설계 원문 §6 "비가역 작업 자동 rollback 금지" 를 이 내용으로 고친다.

## 변경 지점

| 위치 | 변경 |
|---|---|
| `builder_detection` deploy 모델 | `preDeployCommand` 필드 추가 |
| 렌더러 | `pre-deploy.json` Job |
| `ArgoAppStatus` | `pre_deploy_failed` |
| `evaluate_release` | 3행 실패를 hook 실패면 `PRE_DEPLOY_FAILED`, 아니면 `DEPLOY_FAILED` 로 나눈다. **4행 성공에 `operation_contained` + phase `Succeeded` 조건을 되살린다**(MVP 에서 hook 이 없어 뺐던 조건) |
| `deadline_at` | `+ pre-deploy 제한(600초)` |
| ROLLBACK revert 트리 | `pre-deploy.json` 을 뺀다(되돌릴 때 이전 버전 명령을 다시 돌리지 않는다) |
| AppProject whitelist · Prod ClusterRole | `Job` 추가 |
| `FailureCode` · 용어 사전 | `PRE_DEPLOY_FAILED` |

## 사용자 문서에 적을 요구 사항

- 명령은 멱등해야 한다. sync 때마다 다시 돈다.
- 마이그레이션은 이전 버전 코드와 호환되게(expand → contract) 작성한다. 자동 rollback 이 DB 를 되돌리지 않는다.
- 셸이 없는 이미지(distroless 등)는 실패한다.

## 테스트

- 렌더링 스냅숏
- revert 트리에 `pre-deploy.json` 이 없음
- 샘플(pre-deploy 실패)로 실패 시 Deployment 가 그대로인지 E2E 확인

## Task 0 확인

- hook 이 실패하면 wave 0 을 적용하지 않고 phase=Failed 가 되는지
- `syncResult` 로 hook 실패를 구분할 수 있는지

---

## 제안 (원본 계획에 없음)

- revert 트리에서 파일 하나 빼기는 `create_tree(base_tree=<A 의 subtree SHA>, tree=[{path: "pre-deploy.json", sha: null}])` 로 한다. 내용을 다시 렌더링하지 않아도 된다.
