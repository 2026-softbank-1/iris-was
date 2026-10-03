"""수정 에이전트(iris_code_fix_agent)에 넘길 실패 당시의 입력. 원문 그대로 전달한다."""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.response import ApiModel


class RepairSource(ApiModel):
    commit_sha: str = Field(description="실패한 배포가 빌드한 40자리 커밋 SHA")
    root_directory: str = Field(description="저장소 루트 기준 서비스 위치. 루트면 `.`")
    download_url: str = Field(
        description="소스 스냅샷 S3 presigned URL. 로그·저장소에 남기지 않는다."
    )
    expires_at: datetime
    archive_sha256: str | None = Field(
        default=None,
        description="스냅샷을 올릴 때 고정한 tar.gz 해시. 없으면 소스를 고정으로 보지 않는다.",
    )
    manifest_sha256: str | None = Field(
        default=None,
        description="스냅샷을 올릴 때 고정한 파일 목록 해시. 지원하지 않는 아카이브면 없다.",
    )


class RepairContextResponse(ApiModel):
    repository_url: str
    branch: str = Field(description="서비스가 배포하는 브랜치. 수정 PR의 대상이다.")
    auto_deploy: bool
    deployment_id: int
    diagnosis_id: int
    source: RepairSource
    diagnosis_result: dict[str, Any] = Field(
        description="에이전트가 돌려준 원본 diagnosis-result.v3. 키 이름과 값을 바꾸지 않는다."
    )
