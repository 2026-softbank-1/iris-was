from typing import Any

import httpx
import pytest

from app.clients.argocd_client import ArgoAppStatus, ArgoCdClient
from app.core.exceptions import ExternalError

GITOPS_REPOSITORY = "org/gitops-environments"


def _multi_source_body() -> dict[str, Any]:
    return {
        "spec": {
            "sources": [
                {
                    "repoURL": "123.dkr.ecr.ap-northeast-2.amazonaws.com/helm",
                    "chart": "iris-service",
                },
                {"repoURL": "https://github.com/Org/gitops-environments.git", "ref": "values"},
            ]
        },
        "status": {
            "sync": {"status": "Synced", "revisions": ["0.1.0", "c42"]},
            "health": {"status": "Healthy"},
            "operationState": {
                "phase": "Failed",
                "message": "hook failed",
                "syncResult": {"revisions": ["0.1.0", "c41"]},
            },
        },
    }


def test_from_response_multi_source_picks_gitops_revision() -> None:
    status = ArgoAppStatus.from_response(_multi_source_body(), GITOPS_REPOSITORY)

    assert status == ArgoAppStatus("Synced", "c42", "Healthy", "Failed", "c41", "hook failed")


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/Org/gitops-environments.git",
        "git@github.com:org/gitops-environments.git",
        "ssh://git@github.com/org/gitops-environments",
    ],
)
def test_from_response_gitops_repo_url_forms_match(repo_url: str) -> None:
    body = _multi_source_body()
    body["spec"]["sources"][1]["repoURL"] = repo_url

    assert ArgoAppStatus.from_response(body, GITOPS_REPOSITORY).sync_revision == "c42"


def test_from_response_single_source_uses_revision() -> None:
    body = {
        "spec": {"source": {"repoURL": "https://github.com/org/gitops-environments"}},
        "status": {"sync": {"status": "OutOfSync", "revision": "c7"}, "health": {}},
    }

    status = ArgoAppStatus.from_response(body, GITOPS_REPOSITORY)

    assert (status.sync_status, status.sync_revision, status.operation_revision) == (
        "OutOfSync",
        "c7",
        None,
    )


def _client(status_code: int) -> ArgoCdClient:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code)),
        base_url="http://argocd.test",
    )
    return ArgoCdClient(http, GITOPS_REPOSITORY)


@pytest.mark.parametrize("status_code", [403, 404])
async def test_get_application_missing_returns_none(status_code: int) -> None:
    assert await _client(status_code).get_application("svc-12") is None


async def test_get_application_server_error_raises_external() -> None:
    with pytest.raises(ExternalError):
        await _client(503).get_application("svc-12")
