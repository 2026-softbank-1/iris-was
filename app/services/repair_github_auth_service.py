"""WAS 로그인과 GitHub App 설치를 코드수정 코디네이터 인증에 재사용한다."""

from app.core.exceptions import ConflictError, ForbiddenError, ServiceNotFoundError
from app.repositories.service_repository import ServiceRepository
from app.schemas.repair import RepairAccessResponse, RepairGithubTokenResponse
from app.services.repository_url import parse_repository_url
from app.services.source_repository_service import SourceRepositoryService


class RepairGithubAuthService:
    def __init__(self, services: ServiceRepository, repositories: SourceRepositoryService) -> None:
        self._services = services
        self._repositories = repositories

    async def check_access(self, user_id: int, service_id: int) -> RepairAccessResponse:
        service = await self._services.find_by_id_and_owner_id(service_id, user_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        owner, name = parse_repository_url(service.source_repository_url)
        repository = f"{owner}/{name}"
        url = "https://github.com/settings/installations"
        try:
            candidate = await self._repositories.get_repository(user_id, repository)
            installations = await self._repositories.search_installations(user_id)
            installation = next(
                i for i in installations if i.installation_id == candidate.installation_id
            )
            prefix = (
                f"organizations/{owner}/settings"
                if installation.account_type == "Organization"
                else "settings"
            )
            url = f"https://github.com/{prefix}/installations/{candidate.installation_id}"
            # The token is deliberately discarded. The browser receives no credential.
            await self._repositories.create_repair_token(user_id, repository)
        except ForbiddenError as exc:
            return RepairAccessResponse(
                repository=repository, can_write=False, installation_url=url, reason=exc.code
            )
        return RepairAccessResponse(repository=repository, can_write=True, installation_url=url)

    async def issue_token(
        self, user_id: int, service_id: int, expected_repository: str
    ) -> RepairGithubTokenResponse:
        service = await self._services.find_by_id_and_owner_id(service_id, user_id)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=service_id)
        owner, name = parse_repository_url(service.source_repository_url)
        repository = f"{owner}/{name}"
        if repository.lower() != expected_repository.lower():
            raise ConflictError("service source repository changed", service_id=service_id)
        token = await self._repositories.create_repair_token(user_id, repository)
        return RepairGithubTokenResponse(
            repository=repository, token=token.token, expires_at=token.expires_at
        )
