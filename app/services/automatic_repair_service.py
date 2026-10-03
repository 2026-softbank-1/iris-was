"""A user click authorizes generation, hotfix publication and merge without another prompt."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.repair_publication_client import RepairError
from app.core.exceptions import AppError, ConflictError
from app.models.base import now_utc
from app.models.deployment_repair import DeploymentRepair
from app.repositories.deployment_repair_repository import DeploymentRepairRepository
from app.services.repair_publication_service import RepairPublicationService
from app.services.repair_service import RepairService, StartedRepair

logger = logging.getLogger(__name__)


class AutomaticRepairService:
    def __init__(
        self,
        session: AsyncSession,
        repairs: DeploymentRepairRepository,
        candidates: RepairService,
        publication: RepairPublicationService,
    ) -> None:
        self._session = session
        self._repairs = repairs
        self._candidates = candidates
        self._publication = publication

    async def start(
        self, owner_id: int, service_id: int, deployment_id: int, diagnosis_id: int, key: str
    ) -> StartedRepair:
        await self._candidates._get_owned_service(owner_id, service_id)
        diagnosis = await self._candidates._diagnoses.get_by_id(diagnosis_id)
        raw = diagnosis.result or {}
        plans = (raw.get("analysis") or {}).get("remediation", {}).get("plans", [])
        plan_ids = [
            p["id"]
            for p in plans
            if p.get("changes") and all(c.get("kind") == "code" for c in p["changes"])
        ]
        if not plan_ids:
            raise RepairError("NO_CODE_PLAN", "No source-code repair plan", 409)
        existing = await self._repairs.find_by_service_id_and_key(service_id, key)
        if existing is not None:
            # Replaying a completed merge must not fail because main now includes that merge.
            return await self._candidates.start_repair(
                owner_id, service_id, deployment_id, diagnosis_id, plan_ids, key, auto_merge=True
            )
        request = await self._candidates._deployments.find_by_id_and_service_id(
            deployment_id, service_id
        )
        if request is None:
            raise ConflictError("deployment not found")
        self._candidates._validate_selection(request, diagnosis, plan_ids)
        await self._publication.preflight(owner_id, service_id, request.source_sha)
        return await self._candidates.start_repair(
            owner_id, service_id, deployment_id, diagnosis_id, plan_ids, key, auto_merge=True
        )

    async def resume(self, owner_id: int, service_id: int, repair_id: int) -> DeploymentRepair:
        repair = await self._candidates.get_repair(owner_id, service_id, repair_id)
        if (repair.request_metadata.get("publication") or {}).get("status") == "MERGED":
            return repair
        if repair.status == "FAILED":
            raise ConflictError("generation failed; start a new automatic repair attempt")
        await self._publication.preflight(owner_id, service_id)
        repair = await self._repairs.lock_publication(repair_id)
        metadata = dict(repair.request_metadata)
        publication = dict(metadata.get("publication") or {})
        if publication.get("status") != "MERGED":
            publication.update(status="QUEUED")
            publication.pop("errorCode", None)
            metadata.update(
                autoMerge=True,
                autoDeadlineAt=(now_utc() + timedelta(minutes=30)).isoformat(),
                publication=publication,
            )
            repair.request_metadata = metadata
            await self._session.commit()
        return repair

    async def advance(self, owner_id: int, service_id: int, repair_id: int) -> None:
        repair = await self._candidates.get_repair(owner_id, service_id, repair_id)
        if not repair.request_metadata.get("autoMerge"):
            return  # An old candidate or opening the page never grants merge authority.
        publication = repair.request_metadata.get("publication") or {}
        if publication.get("status") in {"MERGED", "ERROR", "SKIPPED"}:
            return
        if datetime.fromisoformat(repair.request_metadata["autoDeadlineAt"]) < now_utc():
            repair.request_metadata = {
                **repair.request_metadata,
                "publication": {**publication, "status": "ERROR", "errorCode": "DEADLINE_EXCEEDED"},
            }
            await self._session.commit()
            return
        if repair.status == "RUNNING":
            repair = await self._candidates.run_repair(owner_id, service_id, repair_id)
        if repair.status != "SUCCEEDED":
            return
        if (repair.result or {}).get("status") != "candidate_ready":
            repair.request_metadata = {
                **repair.request_metadata,
                "publication": {"status": "SKIPPED"},
            }
            await self._session.commit()
            return
        try:
            if not (repair.request_metadata.get("publication") or {}).get("pullUrl"):
                repair = await self._publication.execute(owner_id, service_id, repair_id, "publish")
            await self._publication.execute(owner_id, service_id, repair_id, "merge")
        except AppError as exc:
            repair = await self._repairs.get_by_id(repair_id)
            publication = dict(repair.request_metadata.get("publication") or {})
            if exc.code == "MERGE_BLOCKED":
                # Required CI can finish later; retry this same PR without another model call.
                publication.update(status="WAITING_CHECKS", errorCode="MERGE_BLOCKED")
            elif publication.get("errorCode") == "GITHUB_OUTCOME_UNKNOWN":
                publication.update(status="RECOVERING")
            elif exc.fields.get("publication_busy"):
                return  # Another replica holds the publication lock; it continues the same action.
            else:
                publication.update(status="ERROR", errorCode=exc.code)
            repair.request_metadata = {**repair.request_metadata, "publication": publication}
            await self._session.commit()

    async def pending(self) -> list[tuple[int, int, int]]:
        return [
            (r.requested_by, r.service_id, r.id)
            for r in await self._repairs.pending_automatic()
            if r.requested_by is not None
        ]


AutomaticRepairOpener = Callable[[], AbstractAsyncContextManager[AutomaticRepairService]]


class AutomaticRepairRunner:
    def __init__(self, open_service: AutomaticRepairOpener, interval_seconds: float = 5) -> None:
        self._open_service = open_service
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="automatic-repair")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._task, timeout=20)
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._open_service() as service:
                    jobs = await service.pending()
                for owner_id, service_id, repair_id in jobs:
                    if self._stop.is_set():
                        break
                    try:
                        async with self._open_service() as service:
                            await service.advance(owner_id, service_id, repair_id)
                    except AppError:
                        continue
            except Exception:
                logger.exception("automatic repair iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
