"""Publish an owned, sealed candidate through explicit PR and merge actions."""

import base64
import json
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.repair_publication_client import GitHubPublisher, RepairError
from app.core.exceptions import AppError, ConflictError, ExternalError
from app.models.deployment_repair import DeploymentRepair
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from app.repositories.service_repository import ServiceRepository
from app.services.repair_github_auth_service import RepairGithubAuthService
from app.services.repair_service import RepairService
from app.services.repair_source import canonical_json, sha256
from app.services.repository_url import parse_repository_url


def verify_candidate(repair: DeploymentRepair, artifacts: dict[str, bytes]) -> list[dict[str, Any]]:
    result = repair.result or {}
    try:
        changes = json.loads(artifacts["changes.json"])
        manifest = json.loads(artifacts["manifest.json"])
        files = changes["files"]
        summaries = [{k: v for k, v in f.items() if k != "contentBase64"} for f in files]
        digest = sha256(
            canonical_json(
                {
                    "changes": files,
                    "patch": artifacts["patch.diff"].decode("utf-8"),
                    "manifestSha256": manifest["candidateManifestSha256"],
                }
            )
        )
        if (
            changes.get("schemaVersion") != "iris.repair-changes.v1"
            or manifest.get("schemaVersion") != "iris.repair-manifest.v1"
            or manifest.get("baseCommitSha") != repair.source_sha
            or manifest.get("candidateDigest") != result.get("candidateDigest")
            or digest != result.get("candidateDigest")
            or manifest.get("candidateManifestSha256") != result.get("candidateManifestSha256")
            or summaries != manifest.get("files")
            or summaries != result.get("changedFiles")
            or not 1 <= len(files) <= 5
        ):
            raise ValueError
        size = 0
        for file in files:
            content = base64.b64decode(file["contentBase64"], validate=True)
            size += len(content)
            if sha256(content) != file["afterSha256"]:
                raise ValueError
            root = repair.root_directory
            if root != "." and not file["path"].startswith(root + "/"):
                raise ValueError
        if size > 65536:
            raise ValueError
        return list(files)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RepairError(
            "INVALID_CANDIDATE", "Repair artifacts do not match the sealed candidate"
        ) from None


class RepairPublicationService:
    def __init__(
        self,
        session: AsyncSession,
        repairs: DeploymentRepairRepository,
        services: ServiceRepository,
        candidates: RepairService,
        auth: RepairGithubAuthService,
        api_base_url: str,
    ) -> None:
        self._session = session
        self._repairs = repairs
        self._services = services
        self._candidates = candidates
        self._auth = auth
        self._api_base_url = api_base_url

    async def execute(
        self, owner_id: int, service_id: int, repair_id: int, action: Literal["publish", "merge"]
    ) -> DeploymentRepair:
        repair = await self._candidates.get_repair(owner_id, service_id, repair_id)
        if repair.status != "SUCCEEDED" or (repair.result or {}).get("status") != "candidate_ready":
            raise ConflictError("repair has no publishable candidate")
        artifacts = {}
        if action == "publish":
            for name in ("changes.json", "manifest.json", "patch.diff"):
                artifacts[name] = await self._candidates.get_artifact(
                    owner_id, service_id, repair_id, name
                )
        # Do not commit/release this row lock until publication completes or pauses.
        # Duplicate clicks and requests across WAS replicas cannot perform concurrent writes.
        repair = await self._repairs.lock_publication(repair_id)
        publication = dict(repair.request_metadata.get("publication") or {})
        if publication.get("status") == "MERGED" or (
            action == "publish" and publication.get("pullUrl")
        ):
            await self._session.commit()
            return repair
        service = await self._services.find_by_id_and_owner_id(service_id, owner_id)
        if (
            service is None
            or service.source_branch != "main"
            or service.source_repository_url != repair.source_repository_url
        ):
            await self._session.rollback()
            raise ConflictError("repair requires the original service repository on main")
        owner, name = parse_repository_url(repair.source_repository_url)
        repository = f"{owner}/{name}"
        branch = f"hotfix/iris/{repair.agent_request_id}"
        try:
            token = await self._auth.issue_token(owner_id, service_id, repository)
            async with httpx.AsyncClient(
                base_url=self._api_base_url,
                timeout=15,
                follow_redirects=False,
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "Accept": "application/vnd.github+json",
                },
            ) as http:
                publisher = GitHubPublisher(http)
                if action == "publish":
                    files = verify_candidate(repair, artifacts)
                    if not publication.get("commitSha"):
                        publication["commitSha"] = await publisher.prepare(
                            repository,
                            "main",
                            repair.source_sha,
                            files,
                            f"fix: IRIS repair candidate {repair.agent_request_id}",
                            repair.created_at.isoformat(),
                        )
                    await publisher.publish(repository, branch, publication["commitSha"])
                    publication["pullUrl"] = await publisher.open_pull_request(
                        repository,
                        branch,
                        "main",
                        f"fix: IRIS repair candidate {repair.agent_request_id}",
                        (
                            "AI code repair for a failed deployment.\n\nValidation: not run. "
                            "Review changes and required checks before merging."
                        ),
                        draft=False,
                    )
                    publication.update(status="PR_OPENED", branch=branch)
                else:
                    if not publication.get("pullUrl") or not publication.get("commitSha"):
                        raise ConflictError("publish the repair PR before merging")
                    publication["mergeCommitSha"] = await publisher.merge_pull_request(
                        repository,
                        publication["pullUrl"],
                        branch,
                        "main",
                        publication["commitSha"],
                        repair.source_sha,
                    )
                    publication["status"] = "MERGED"
                publication.pop("errorCode", None)
        except (AppError, httpx.HTTPError) as exc:
            # Keep known commit/PR identifiers for reconciliation; never store credentials.
            publication.update(
                status="ERROR",
                errorCode=exc.code if isinstance(exc, AppError) else "GITHUB_OUTCOME_UNKNOWN",
            )
            repair.request_metadata = {**repair.request_metadata, "publication": publication}
            await self._session.commit()
            if isinstance(exc, AppError):
                raise
            raise ExternalError(
                "github publication outcome is uncertain; retry the same repair"
            ) from None
        repair.request_metadata = {**repair.request_metadata, "publication": publication}
        await self._session.commit()
        return repair
