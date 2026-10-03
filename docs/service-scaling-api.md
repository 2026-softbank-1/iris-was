# 서비스 Pod 수·리소스 설정 API

`PUT /api/v1/services/{serviceId}/scaling`으로 배포된 서비스의 Pod 수와 Pod별 CPU·메모리 사양 전체를 교체한다. 소유자만 접근하며 쿠키 또는 Bearer 토큰으로 인증한다.

```http
PUT /api/v1/services/12/scaling
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: scale-12-20261003

{
  "replicas": 2,
  "resources": {
    "requests": {"cpu": "250m", "memory": "256Mi"},
    "limits": {"cpu": "1", "memory": "1Gi"}
  }
}
```

```json
{
  "success": true,
  "data": {
    "serviceId": 12,
    "replicas": 2,
    "resources": {
      "requests": {"cpu": "250m", "memory": "256Mi"},
      "limits": {"cpu": "1", "memory": "1Gi"}
    },
    "deploymentRequestId": 34
  }
}
```

응답은 `202 Accepted`다. 현재 성공한 배포의 이미지를 재사용하는 `RESTART` 요청을 만들고 `DEPLOY` job부터 실행한다. 소스 빌드는 다시 하지 않는다. `GET /api/v1/services/12/deployments/34`로 완료·실패를 확인한다. 기존 재시작 경로와 같아서 Pod가 새로 시작되고 환경변수는 현재 서비스 변수로 스냅샷된다.

`GET /api/v1/services/{serviceId}/scaling`은 저장된 **원하는 설정**을 반환한다. 실시간 Pod 수나 적용 완료를 뜻하지 않는다. 설정이 없는 기존 서비스는 iris-infra 기본값인 `replicas: 1`, requests `100m / 256Mi`, limits `1 / 512Mi`를 반환한다. GET 응답에는 `deploymentRequestId`가 없다.

## 입력 규칙

- `replicas`는 JSON 정수 0~10이다. 0이면 Pod가 0개가 되어 요청을 처리하지 못하며 Application·Service·Ingress는 유지된다. 완전한 배포 제거는 기존 `REMOVE` 요청을 쓴다.
- `resources.requests`와 `resources.limits`의 CPU·memory를 모두 보낸다. 알 수 없는 필드는 거부한다.
- CPU는 양수 문자열이며 코어(`"1"`, `"0.5"`, 소수점 최대 3자리) 또는 정수 millicore(`"250m"`)로 표현한다.
- 메모리는 양수 문자열이며 bytes(`"536870912"`), binary 단위(`Ki`, `Mi`, `Gi`, `Ti`, `Pi`, `Ei`) 또는 decimal 단위(`k`, `M`, `G`, `T`, `P`, `E`)를 쓴다. 소수점 최대 3자리까지 받는다.
- CPU와 memory의 requests는 각각 limits 이하여야 한다. `"1"`과 `"1000m"`, `"1Gi"`와 `"1024Mi"`처럼 단위가 달라도 실제 수량으로 비교한다.
- 이 사양은 Pod 컨테이너 자원이다. EC2 노드의 instance type을 바꾸는 필드는 없다.

CPU·메모리 단위는 [Kubernetes 리소스 수량 문서](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)를 따른다. 위에 명시한 표현만 API에서 받는다.

## 중복·충돌·실패

| 조건 | 응답 |
|---|---|
| 로그인 없음 | `401 UNAUTHORIZED` |
| 다른 소유자 또는 삭제된 서비스·프로젝트 | `404 SERVICE_NOT_FOUND` |
| 배포가 진행 중 | `409 DEPLOYMENT_IN_PROGRESS` |
| 성공한 배포가 없거나 마지막 성공이 REMOVE | `409 NO_SUCCEEDED_DEPLOYMENT` |
| 잘못된 수량·누락·추가 필드 | `422 VALIDATION_ERROR` |
| 현재 배포에 재사용할 이미지가 없음 | `422 INVALID_INPUT` |
| 같은 Idempotency-Key에 다른 설정을 전송 | `422 INVALID_INPUT` |

`Idempotency-Key`는 선택이며 1~64자다. 같은 서비스에 같은 키·본문으로 재전송하면 처음 생성한 배포 요청 ID를 반환한다. 이후 다른 설정을 저장했더라도 이전 키의 재전송은 현재 설정을 되돌리지 않는다. 키를 보내면 변경 없는 설정도 추적 가능한 배포 요청을 한 번 생성한다.

키 없이 이미 저장되고 마지막 성공한 배포에도 적용된 설정을 다시 보내면 새 job 없이 그 성공한 배포 ID를 반환한다. 적용에 실패한 설정은 같은 본문으로 다시 요청할 수 있다. 진행 중인 요청이 있으면 키 없는 재전송도 409다.

설정 저장과 배포 요청·job 생성은 한 트랜잭션이다. 접수 실패는 설정을 바꾸지 않는다. 접수 뒤 적용 실패는 기존 Deploy Worker의 이전 GitOps 디렉터리 복구를 따른다. 원하는 설정은 저장되어 있어 GET 결과와 실제 실행 사양이 다를 수 있으므로 배포 상태를 함께 확인한다.

## 인프라 연동

기준은 `iris-infra-pipeline/helm/charts/iris-service/values.yaml`, `values.schema.json`, `templates/deployment.yaml`이다. Worker는 `services/{serviceId}/prod/values.yaml`의 `replicas`와 `resources`를 쓰고 Argo CD가 Deployment에 적용한다. chart 변경은 필요 없다.

Service의 `scaling_config`를 모든 배포 요청의 `scaling_snapshot`에 복사한다. 수동·push·재배포·재시작·롤백은 요청 시점의 원하는 사양을 사용하므로 이후 빌드에도 설정이 유지된다. 롤백은 이미지와 환경변수를 원본으로 되돌리면서 사양은 현재 원하는 설정을 유지한다. 기존 요청의 null 스냅샷은 chart 기본값을 사용한다.

API와 Worker를 올리기 전에 `uv run alembic upgrade head`로 migration `71f880d9c6ae`를 적용한다.
