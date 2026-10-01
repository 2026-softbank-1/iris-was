from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from app.schemas.response import ApiModel
from app.services.project_service import ProjectSummary

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ProjectDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class ProjectCreateRequest(ApiModel):
    name: ProjectName
    description: ProjectDescription | None = None


class ProjectUpdateRequest(ApiModel):
    """보낸 필드만 바꾼다. description 은 null 로 비울 수 있다."""

    name: ProjectName | None = None
    description: ProjectDescription | None = None


class ProjectResponse(ApiModel):
    id: int
    name: str
    description: str | None = None
    service_count: int
    online_service_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_summary(cls, summary: ProjectSummary) -> "ProjectResponse":
        project = summary.project
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            service_count=summary.counts.service_count,
            online_service_count=summary.counts.online_service_count,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
