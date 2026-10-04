"""GitHub 웹훅 payload 중 우리가 쓰는 필드만 선언한다. 나머지는 무시한다."""

from pydantic import BaseModel, Field

from app.schemas.response import ApiModel


class GithubCommit(BaseModel):
    message: str = ""
    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class GithubWebhookRepository(BaseModel):
    html_url: str


class GithubPushEvent(BaseModel):
    ref: str
    after: str
    deleted: bool = False
    head_commit: GithubCommit | None = None
    commits: list[GithubCommit] = Field(default_factory=list)
    repository: GithubWebhookRepository


class GithubInstallationAccount(BaseModel):
    login: str
    type: str


class GithubInstallationDetail(BaseModel):
    id: int
    account: GithubInstallationAccount


class GithubInstallationEvent(BaseModel):
    action: str
    installation: GithubInstallationDetail


class WebhookReceiptResponse(ApiModel):
    is_handled: bool = Field(description="이 이벤트로 처리한 일이 있었는지", examples=[True])
    deployment_request_ids: list[int] = Field(
        default_factory=list, description="push 로 새로 만든 배포 요청 ID", examples=[[12]]
    )
    repository_analysis_ids: list[int] = Field(
        default_factory=list,
        description="스택 레포 push 로 같은 커밋에 다시 접수한 레포 구성 분석 ID",
        examples=[[7]],
    )
