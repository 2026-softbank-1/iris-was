import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.exceptions import ExternalError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArgoAppStatus:
    """Argo CD Application 상태. 값은 Argo 원본 표기 그대로다(Synced·Healthy·Failed 등).

    revision 은 GitOps 저장소 소스의 커밋이다. multi-source(Helm chart + values) Application 은
    소스별 revisions 배열을 주므로, spec.sources 에서 GitOps 저장소의 위치를 찾아 그 값을 쓴다.
    """

    sync_status: str | None
    sync_revision: str | None
    health_status: str | None
    operation_phase: str | None
    operation_revision: str | None
    operation_message: str | None

    @classmethod
    def from_response(cls, body: dict[str, Any], gitops_repository: str) -> "ArgoAppStatus":
        spec = body.get("spec", {})
        status = body.get("status", {})
        sync = status.get("sync", {})
        operation = status.get("operationState", {})
        sync_result = operation.get("syncResult", {})
        index = _source_index(spec, gitops_repository)
        return cls(
            sync_status=sync.get("status"),
            sync_revision=_revision(sync, index),
            health_status=status.get("health", {}).get("status"),
            operation_phase=operation.get("phase"),
            operation_revision=_revision(sync_result, index),
            operation_message=operation.get("message"),
        )


class ArgoCdClient:
    """Argo CD REST API 읽기 전용 Client. http 에 base_url·Bearer 토큰을 설정해 넘긴다."""

    def __init__(self, http: httpx.AsyncClient, gitops_repository: str) -> None:
        self._http = http
        self._gitops_repository = gitops_repository

    async def get_application(self, name: str, refresh: bool = False) -> ArgoAppStatus | None:
        """Application 이 없으면 None. refresh 면 Git 을 다시 읽은 뒤의 상태를 돌려준다."""
        params = {"refresh": "normal"} if refresh else {}
        try:
            response = await self._http.get(f"/api/v1/applications/{name}", params=params)
        except httpx.HTTPError as exc:
            raise ExternalError("argocd request failed", application=name) from exc
        # ponytail: Argo 는 없는 Application 을 403 으로 숨길 수 있어 403·404 를 "없음"으로 본다.
        #   토큰·RBAC 설정 오류도 "없음"으로 보여 deadline 초과(DEPLOY_TIMED_OUT)로 드러난다.
        if response.status_code in (403, 404):
            return None
        if response.is_error:
            raise ExternalError(
                "argocd request failed", application=name, status_code=response.status_code
            )
        return ArgoAppStatus.from_response(response.json(), self._gitops_repository)


def _source_index(spec: dict[str, Any], gitops_repository: str) -> int | None:
    """spec.sources 에서 GitOps 저장소({owner}/{repo})의 위치. 단일 소스면 None.

    repoURL 은 https 와 SSH(`git@github.com:{owner}/{repo}.git`) 를 모두 받는다.
    """
    sources = spec.get("sources") or []
    suffix = f"/{gitops_repository.lower()}"
    for index, source in enumerate(sources):
        repo_url = str(source.get("repoURL", "")).lower().replace(":", "/")
        if repo_url.removesuffix("/").removesuffix(".git").endswith(suffix):
            return index
    if sources:
        # revision 을 못 읽으면 반영 여부를 알 수 없어 모든 배포가 기한 초과로 끝난다.
        logger.warning(
            "gitops source not found in application",
            extra={"action": "get_application", "gitops_repository": gitops_repository},
        )
    return None


def _revision(section: dict[str, Any], index: int | None) -> str | None:
    if index is None:
        revision: str | None = section.get("revision")
        return revision
    revisions = section.get("revisions") or []
    return revisions[index] if index < len(revisions) else None
