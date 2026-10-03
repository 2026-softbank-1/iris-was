import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.repair_publication_client import RepairError
from app.core.exceptions import ConflictError, ForbiddenError, ServiceNotFoundError
from app.services.repair_publication_service import RepairPublicationService, verify_candidate
from app.services.repair_source import canonical_json, sha256
from tests.fakes_repair import OWNER, RepairSetup


@pytest.fixture
async def publication_setup(monkeypatch):
    setup = await RepairSetup().build()
    setup.service.source_branch = "main"
    deployment, diagnosis = setup.add_repair_inputs()
    candidates = setup.repair_service()
    started = await candidates.start_repair(
        OWNER, setup.service.id, deployment, diagnosis, ["R1"], "publish-test"
    )
    setup.repair_agent.candidate = True
    before, after = b"broken\n", b"fixed\n"
    files = [
        {
            "path": "src/app.py",
            "operation": "update",
            "mode": "100644",
            "contentBase64": base64.b64encode(after).decode(),
            "beforeSha256": sha256(before),
            "afterSha256": sha256(after),
        }
    ]
    summaries = [{k: v for k, v in f.items() if k != "contentBase64"} for f in files]
    patch = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-broken\n+fixed\n"
    digest = sha256(canonical_json({"changes": files, "patch": patch, "manifestSha256": "d" * 64}))
    artifacts = {
        "patch.diff": patch.encode(),
        "changes.json": canonical_json({"schemaVersion": "iris.repair-changes.v1", "files": files}),
        "manifest.json": canonical_json(
            {
                "schemaVersion": "iris.repair-manifest.v1",
                "baseCommitSha": started.repair.source_sha,
                "candidateManifestSha256": "d" * 64,
                "candidateDigest": digest,
                "files": summaries,
            }
        ),
    }
    setup.repair_agent.artifacts = artifacts
    await candidates.run_repair(OWNER, setup.service.id, started.repair.id)
    repair = started.repair
    repair.result = {**repair.result, "candidateDigest": digest, "changedFiles": summaries}
    auth = AsyncMock()
    auth.issue_token.return_value.token = "ghs_secret_server_only"
    calls = []
    refs = {"main": repair.source_sha}
    pulls = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer ghs_secret_server_only"
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        if "/git/ref/heads/" in path:
            branch = path.split("heads/", 1)[1]
            return (
                httpx.Response(200, json={"object": {"sha": refs[branch]}})
                if branch in refs
                else httpx.Response(404, json={})
            )
        if "/contents/" in path:
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(before).decode(),
                },
            )
        if path.endswith("git/refs"):
            refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if path.endswith("pulls"):
            if request.method == "GET":
                return httpx.Response(200, json=pulls)
            record = {
                "head": {"ref": body["head"], "sha": "b" * 40, "repo": {"full_name": "o/r"}},
                "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                "html_url": "https://github.com/o/r/pull/1",
                "state": "open",
                "draft": False,
            }
            pulls.append(record)
            return httpx.Response(201, json=record)
        if path.endswith("pulls/1"):
            return httpx.Response(200, json=pulls[0])
        if path.endswith("pulls/1/merge"):
            assert body["sha"] == "b" * 40
            pulls[0].update(merged=True, merge_commit_sha="m" * 40)
            refs["main"] = "m" * 40
            return httpx.Response(200, json={"merged": True, "sha": "m" * 40})
        return httpx.Response(200, json={"sha": "b" * 40, "tree": {"sha": "original-tree"}})

    # The fixture repository matches GitHub's mocked repository identity.
    repair.source_repository_url = setup.service.source_repository_url = "https://github.com/o/r"
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.repair_publication_service.httpx.AsyncClient",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)),
    )
    service = RepairPublicationService(
        setup.session, setup.repairs, setup.services, candidates, auth, "https://api.github.com"
    )
    return setup, service, repair, auth, calls, refs, artifacts


async def test_publish_merge_are_explicit_and_idempotent(publication_setup):
    setup, service, repair, auth, calls, refs, _ = publication_setup
    await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert repair.request_metadata["publication"]["status"] == "PR_OPENED"
    assert refs["main"] == repair.source_sha
    count = len(calls)
    await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert len(calls) == count
    await service.execute(OWNER, setup.service.id, repair.id, "merge")
    assert repair.request_metadata["publication"]["status"] == "MERGED"
    assert refs["main"] == "m" * 40
    count = len(calls)
    await service.execute(OWNER, setup.service.id, repair.id, "merge")
    assert len(calls) == count
    assert "ghs_" not in str(repair.request_metadata) + str(repair.result)
    assert len(setup.repair_agent.requests) == 1
    auth.issue_token.assert_called_with(OWNER, setup.service.id, "o/r")


async def test_denied_write_can_resume_same_candidate_after_approval(publication_setup):
    setup, service, repair, auth, calls, _, _ = publication_setup
    auth.issue_token.side_effect = ForbiddenError("write permission missing")
    with pytest.raises(ForbiddenError):
        await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert calls == []
    assert repair.request_metadata["publication"]["errorCode"] == "FORBIDDEN"
    auth.issue_token.side_effect = None
    await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert repair.request_metadata["publication"]["status"] == "PR_OPENED"
    assert len(setup.repair_agent.requests) == 1


async def test_publication_requires_owner_and_original_repository_main(publication_setup):
    setup, service, repair, _, calls, _, _ = publication_setup
    with pytest.raises(ServiceNotFoundError):
        await service.execute(OWNER + 1, setup.service.id, repair.id, "publish")
    setup.service.source_branch = "develop"
    with pytest.raises(ConflictError):
        await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert calls == []


async def test_manifest_mismatch_stops_before_github_writes(publication_setup):
    setup, service, repair, _, calls, _, artifacts = publication_setup
    repair.result["candidateDigest"] = "0" * 64
    with pytest.raises(RepairError):
        await service.execute(OWNER, setup.service.id, repair.id, "publish")
    assert calls == []
    assert repair.request_metadata["publication"]["errorCode"] == "INVALID_CANDIDATE"
    with pytest.raises(RepairError):
        verify_candidate(repair, artifacts)


async def test_changed_main_blocks_merge_and_keeps_pr_for_review(publication_setup):
    setup, service, repair, _, calls, refs, _ = publication_setup
    await service.execute(OWNER, setup.service.id, repair.id, "publish")
    refs["main"] = "e" * 40
    with pytest.raises(RepairError) as error:
        await service.execute(OWNER, setup.service.id, repair.id, "merge")
    assert error.value.code == "SOURCE_HEAD_CHANGED"
    assert repair.request_metadata["publication"]["pullUrl"] == "https://github.com/o/r/pull/1"
    assert not any(method == "PUT" for method, _, _ in calls)


async def test_latest_attempt_is_scoped_to_owner_deployment_and_diagnosis(publication_setup):
    setup, _, repair, _, _, _, _ = publication_setup
    candidate = setup.repair_service()
    assert (
        await candidate.latest_repair(
            OWNER, setup.service.id, repair.deployment_request_id, repair.diagnosis_id
        )
    ).id == repair.id
    with pytest.raises(ServiceNotFoundError):
        await candidate.latest_repair(
            OWNER + 1, setup.service.id, repair.deployment_request_id, repair.diagnosis_id
        )
