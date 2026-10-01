from pydantic import BaseModel


class BuildJobPayload(BaseModel):
    """BUILD job 의 입력. 요청 시점의 소스 위치를 고정해 두고 Worker 는 이것만 본다."""

    source_repository_url: str
    source_branch: str
    source_sha: str
    root_directory: str | None = None
