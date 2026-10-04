import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.clients.observability_client import LogEntry
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


# Pod 로그는 Argo CD 가 tailnet 너머 클러스터의 kubelet 에서 읽어 오므로 기본 10초보다 길게 둔다.
POD_LOG_TIMEOUT_SECONDS = 30.0
# Argo 가 주는 RFC3339Nano 시각(`2026-10-04T01:02:03.123456789Z`). 소수부는 끝의 0 이 잘린다.
_RFC3339_NANO = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


class PodLogClient(Protocol):
    async def search_pod_logs(
        self,
        application: str,
        namespace: str,
        container: str,
        since_seconds: int,
        tail_lines: int,
    ) -> list[LogEntry]: ...


class ArgoCdClient:
    """Argo CD REST API 읽기 전용 Client.

    Worker 는 http 에 base_url·Bearer 토큰을 설정해 넘긴다. Control API 는 공용 http 를 쓰므로
    base_url·token 을 따로 넘긴다.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        gitops_repository: str = "",
        *,
        base_url: str = "",
        token: SecretStr | None = None,
    ) -> None:
        self._http = http
        self._gitops_repository = gitops_repository
        self._base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            return {}
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}

    async def get_application(self, name: str, refresh: bool = False) -> ArgoAppStatus | None:
        """Application 이 없으면 None. refresh 면 Git 을 다시 읽은 뒤의 상태를 돌려준다."""
        params = {"refresh": "normal"} if refresh else {}
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v1/applications/{name}",
                params=params,
                headers=self._headers(),
            )
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

    async def search_pod_logs(
        self,
        application: str,
        namespace: str,
        container: str,
        since_seconds: int,
        tail_lines: int,
    ) -> list[LogEntry]:
        """Application 이 띄운 지금 Pod 들의 로그(오래된 것부터). Application 이 없으면 빈 목록이다.

        tail_lines 는 Pod 마다 적용된다. 지워진 Pod 의 로그는 kubelet 에 없어 돌려주지 않는다.
        """
        params = {
            "namespace": namespace,
            "container": container,
            "sinceSeconds": str(max(1, since_seconds)),
            "tailLines": str(tail_lines),
            "follow": "false",
        }
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v1/applications/{application}/logs",
                params=params,
                headers=self._headers(),
                timeout=POD_LOG_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ExternalError("argocd log request failed", application=application) from exc
        # 아직 배포되지 않은 서비스는 Application 이 없다. Argo 는 이를 403 으로 숨길 수 있다.
        if response.status_code in (403, 404):
            logger.warning(
                "argocd application not visible for logs",
                extra={
                    "action": "search_pod_logs",
                    "application": application,
                    "status_code": response.status_code,
                },
            )
            return []
        if response.is_error:
            raise ExternalError(
                "argocd log request failed",
                application=application,
                status_code=response.status_code,
            )
        return _parse_pod_logs(response.text, container, application)


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


def _parse_pod_logs(body: str, container: str, application: str) -> list[LogEntry]:
    """줄마다 `{"result": LogEntry}` 가 온다. 오류는 HTTP 200 뒤 `{"error": ...}` 줄로 온다."""
    entries: list[LogEntry] = []
    try:
        for line in body.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if "error" in item:
                raise ExternalError("argocd log stream failed", application=application)
            result = item["result"]
            if result.get("last"):
                continue
            content = result.get("content", "")
            pod = result.get("podName", "")
            timestamp = result.get("timeStampStr") or result["timeStamp"]
            if not isinstance(content, str) or not isinstance(pod, str):
                raise ValueError("invalid log entry")
            entries.append(LogEntry(str(_to_unix_ns(timestamp)), content, pod, container))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ExternalError("invalid argocd log response", application=application) from exc
    return sorted(entries, key=lambda entry: (int(entry.timestamp_ns), entry.pod, entry.message))


def _to_unix_ns(value: str) -> int:
    match = _RFC3339_NANO.match(value)
    if match is None:
        raise ValueError("invalid log timestamp")
    base, fraction, zone = match.groups()
    seconds = datetime.fromisoformat(base + ("+00:00" if zone == "Z" else zone)).timestamp()
    return int(seconds) * 10**9 + int((fraction or "").ljust(9, "0"))
