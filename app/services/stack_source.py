"""스택 재배포의 앱 소스 커밋: 스택 브랜치의 최신 커밋(GitHub)."""

from app.core.exceptions import InvalidInputError
from app.services.repository_url import parse_repository_url
from app.services.source_repository_service import SourceRepositoryService
from app.services.stack_service import SourceResolver, StackService


def branch_head_resolver(
    source_repository_service: SourceRepositoryService,
    owner_id: int,
    stack_service: StackService,
    stack_id: int,
) -> SourceResolver:
    """앱이 있을 때만 한 번 부른다(DB 만 다시 배포하면 GitHub 를 부르지 않는다)."""

    async def resolve() -> tuple[str, str | None]:
        stack = await stack_service.get_stack_model(stack_id)
        owner, name = parse_repository_url(stack.source_repository_url)
        head = await source_repository_service.find_branch_head(
            owner_id, f"{owner}/{name}", stack.source_branch
        )
        if head is None:
            raise InvalidInputError(
                "branch not found in repository", field="branch", branch=stack.source_branch
            )
        return head.sha, head.message

    return resolve
