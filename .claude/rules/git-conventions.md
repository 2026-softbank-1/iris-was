# Git 컨벤션 (커밋 메시지 · 브랜치명)

- **적용 대상**: 이 레포의 모든 커밋과 브랜치
- **함께 보기**: PR 본문은 [.github/PULL_REQUEST_TEMPLATE.md](../../.github/PULL_REQUEST_TEMPLATE.md) 를 따른다. 이 문서는 **커밋·브랜치**만 다룬다.

> **핵심**: 커밋 메시지는 [Conventional Commits](https://www.conventionalcommits.org/) 를 따른다.

---

## 1. 커밋 메시지

### 형식

```text
<type>: <설명>
```

- `<type>` 은 아래 표의 Conventional Commits 타입.
- `<설명>` 은 한글/영문 모두 허용, 명령형으로 간결하게. 마침표로 끝내지 않는다.

### 예시

```text
feat: 배포 파이프라인 API 구현
fix: 배포 상태 조회 오류 수정
chore: ruff 설정 추가
```

### type 목록

| type       | 용도                                  |
| ---------- | ----------------------------------- |
| `feat`     | 기능 추가                              |
| `fix`      | 버그 수정                             |
| `docs`     | 문서만 변경                           |
| `style`    | 포맷·세미콜론 등 동작 변화 없는 변경     |
| `refactor` | 기능 변화 없는 구조 개선               |
| `test`     | 테스트 추가·수정                       |
| `chore`    | 빌드·설정·의존성 등 부수 작업           |
| `perf`     | 성능 개선                            |

> 본문(body)·꼬리말(footer)이 필요하면 제목 다음 빈 줄 뒤에 작성한다(선택).

---

## 2. 브랜치명

### 형식

```text
<type>/<설명>
```

- `<type>` 은 커밋 type 목록과 같다.
- `<설명>` 은 **소문자 + 하이픈(`-`)** 으로 연결한다. 대문자·공백·언더스코어 금지.

### 예시

```text
feat/deploy-pipeline
fix/release-status
```

---

## 3. 체크리스트

1. 커밋 type 이 표에 있는가? 설명이 명령형·간결한가?
2. 브랜치가 `<type>/<소문자-하이픈-설명>` 형식인가?
