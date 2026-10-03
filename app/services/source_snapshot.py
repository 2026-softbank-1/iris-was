"""배포 요청이 올린 소스 스냅샷을 찾는 규칙. 진단과 수정 에이전트가 같은 소스를 가리키게 한다."""

from datetime import timedelta

from app.models.base import now_utc
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.repositories.build_repository import BuildRepository

# 빌드가 올린 스냅샷은 하루 뒤 지워진다. 그 전까지만 소스를 쓴다.
SNAPSHOT_RETENTION = timedelta(hours=23)


async def find_snapshot_build(
    build_repository: BuildRepository, request: DeploymentRequest, build: Build | None
) -> Build | None:
    """소스 스냅샷을 올린 빌드. 롤백·재시작은 빌드하지 않으므로 원본 요청의 빌드를 따라간다."""
    candidate = build
    if (
        candidate is None or candidate.codebuild_build_id is None
    ) and request.source_deployment_request_id is not None:
        candidate = await build_repository.find_by_deployment_request_id(
            request.source_deployment_request_id
        )
    # CodeBuild 를 시작했다면 스냅샷은 이미 올라가 있다.
    if candidate is None or candidate.codebuild_build_id is None:
        return None
    if now_utc() - candidate.created_at > SNAPSHOT_RETENTION:
        return None
    return candidate
