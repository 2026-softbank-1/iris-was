# 0010. Dockerfile 없는 소스의 빌드는 서비스 소유 Railpack 경로로 넘긴다

- 상태: 제안됨
- 날짜: 2026-10-01
- 결정 근거: 팀 구현 논의와 사용자 요청, 서비스/백엔드 담당 김현겸의 Railpack 사용 방침

## 배경

분석 뒤 Dockerfile을 생성하는 v1 준비 모듈은 서비스 담당자의 Railpack + BuildKit 빌드 경로와 책임이 겹친다. 분석기가 추천한 빌더와 서비스가 실제 확정한 빌더도 응답에서 구분해야 한다.

## 검토한 선택지

1. 분석기가 Dockerfile을 생성하고 WAS가 overlay를 수용한다. 생성 정책·빌드 정책을 양쪽에서 유지해야 한다.
2. 분석기가 소스 분석·빌더 추천·원본 패키징을 담당하고 서비스가 Dockerfile/Railpack 실행을 소유한다. 빌드 결과와 로그의 소유자가 명확하다.

## 결정

2번을 v2 계약으로 구현한다. `buildHandoff`에 서비스 소유권, 요청 builder, 추천 builder, 설정 결정 필요 여부와 이유를 명시한다. 기본 Dockerfile이 없으면 Railpack을 추천하되, 명시적 Dockerfile 선택을 조용히 바꾸지 않는다. 명시적 Railpack은 기존 Dockerfile이 있어도 유지한다.

WAS는 생성 파일을 허용하지 않고 원본 manifest와 아카이브 전체 바이트를 검증한다. builder 미지정 분석 요청도 받을 수 있지만, 실제 빌드는 기존 정책대로 서비스 선택 확정 후 시작한다. `ready`는 소스 준비 상태이며 실행 권한을 부여하지 않는다.

## 결과

- v1의 `allowGeneration`, 생성 origin/template, Dockerfile overlay를 제거한다. v1 호출자는 v2로 함께 변경해야 한다.
- 소스 취득·CodeBuild·Railpack 버전 고정·ECR push·로그 전달은 서비스/백엔드 담당 범위다.
- Dockerfile 부재는 정상 빌더 선택 조건이다. 실제로 검증된 결함만 로그 기반 개선 에이전트의 별도 계약으로 넘긴다.
- 현재 변경에는 Job 큐 연결·클라우드 실행·재귀 수정 실행이 포함되지 않는다.
