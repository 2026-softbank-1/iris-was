# TODO — 사용자 PORT 지정

> [Deploy Worker 계획](deploy-worker-plan.md) MVP 에서 뺀 기능. MVP 는 8080 고정이다.

**목표**: 사용자 `PORT` 변수가 있으면 그 값으로 앱 포트를 맞춘다. 없으면 8080.

## 선행 조건

- [런타임 환경변수](todo-runtime-variables.md)

## 설계

- `resolve_port(variables: dict[str, str]) -> int` — `PORT` 가 있으면 그 값, 없으면 8080. 범위 밖이면 `InvalidInputError`.
- 반영 위치: `env` 의 `PORT`, `containerPort`, readiness probe 포트, Service `targetPort`(`80 → PORT`).
- `PORT` 는 `env` 로 주입하므로 `envFrom` 의 같은 이름 값보다 우선한다. 그래서 반드시 `resolve_port` 결과를 `env` 에 넣는다.

## 테스트

- 사용자 PORT 가 containerPort·probe·Service 에 반영된다
- 범위 밖이면 `InvalidInputError`

---

## 제안 (원본 계획에 없음)

- 검증은 렌더링이 아니라 **변수 저장 시점**(`VariableService`, 신뢰 경계)에 한다. 배포 단계에서 걸리면 돌려줄 실패 코드가 없다. 범위는 1~65535 로 본다.
