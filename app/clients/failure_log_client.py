"""Read failure evidence from server-owned build IDs and persisted Argo messages."""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.diagnosis_config import DiagnosisSettings
from app.core.exceptions import ExternalError, NotConfiguredError
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job


def mask_failure_log(text: str, secret_values: list[str]) -> str:
    try:
        from ai_error_check_agent.preprocessing import normalize, redact
    except ImportError:
        raise NotConfiguredError("diagnosis worker package is unavailable") from None
    normalized = normalize(text)
    # Known snapshot values complement pattern redaction (e.g. a bare password in a trace).
    for secret in sorted(set(secret_values), key=len, reverse=True):
        if secret:
            normalized = normalized.replace(secret, "[REDACTED]")
    return str(redact(normalized))


def _tail(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    # Never invent the original line number after truncating an upstream stream.
    tail = encoded[-limit:].decode("utf-8", errors="ignore")
    if "\n" in tail:
        tail = tail.split("\n", 1)[1]
    return tail, True


class FailureLogClient:
    def __init__(
        self, settings: DiagnosisSettings, *, codebuild: Any = None, cloudwatch: Any = None
    ) -> None:
        self._settings = settings
        self._codebuild = codebuild
        self._cloudwatch = cloudwatch

    async def collect(self, session: AsyncSession, deployment: DeploymentRequest) -> dict[str, Any]:
        build = (
            await session.scalars(select(Build).where(Build.deployment_request_id == deployment.id))
        ).one_or_none()
        jobs = list(
            (
                await session.scalars(
                    select(Job).where(Job.deployment_request_id == deployment.id).order_by(Job.id)
                )
            ).all()
        )
        # Materialize database input before network I/O; never send env snapshots to a model.
        variables = deployment.variables_snapshot or {}
        values = list(variables.values())
        bindings = variables.get("bindings")
        if isinstance(bindings, list):
            values.extend(
                binding["value"]
                for binding in bindings
                if isinstance(binding, dict) and "value" in binding
            )
        secrets = [
            str(value)
            for value in values
            if isinstance(value, (str, int, float)) and len(str(value)) >= 3
        ]
        chunks: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        limitations: list[str] = []
        for job in jobs:
            payload = job.payload or {}
            entries = payload.get("failureLogs")
            if not isinstance(entries, list):
                log = payload.get("failureLog")
                entries = [log] if isinstance(log, dict) else []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                    continue
                text, omitted = _tail(
                    mask_failure_log(entry["text"], secrets), self._settings.log_max_bytes
                )
                if not text.strip():
                    continue
                source = str(entry.get("sourceId") or f"job-{job.id}")
                source_id = "argo-" + hashlib.sha256(source.encode()).hexdigest()[:24]
                chunk_id = f"argo-{job.id}-{len(chunks) + 1}"
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "source_id": source_id,
                        "stage": "deploy",
                        "stream": "combined",
                        "source_line_start": None if omitted else entry.get("sourceLineStart"),
                        "captured_at": None,
                        "is_complete": False,
                        "text": text,
                    }
                )
                sources.append(
                    {
                        "chunk_id": chunk_id,
                        "kind": "argo_operation_message",
                        "job_id": job.id,
                        "artifact_ref": entry.get("artifactRef"),
                    }
                )
                limitations.append("Argo operation message only; runtime pod logs unavailable.")
                if omitted:
                    limitations.append("Argo message tail was truncated to the collection budget.")
        if build and build.codebuild_build_id:
            try:
                result = await self._collect_codebuild(build.codebuild_build_id, secrets)
                if result is not None:
                    chunk, source_metadata, source_limits = result
                    chunks.insert(0, chunk)
                    sources.insert(0, source_metadata)
                    limitations.extend(source_limits)
            except (ExternalError, NotConfiguredError) as error:
                # Argo evidence may still suffice; preserve the missing build-log scope explicitly.
                limitations.append(f"CodeBuild log collection unavailable: {error.code}.")
        total = sum(len(item["text"].encode()) for item in chunks)
        while total > self._settings.log_max_bytes and chunks:
            # Prefer deploy error over successful build output when both are present.
            item = chunks[0]
            others = total - len(item["text"].encode())
            available = max(0, self._settings.log_max_bytes - others)
            if available < 256:
                chunks.pop(0)
                sources.pop(0)
                total = others
            else:
                item["text"], _ = _tail(item["text"], available)
                item["source_line_start"] = None
                item["is_complete"] = False
                total = sum(len(chunk["text"].encode()) for chunk in chunks)
            limitations.append("Combined source logs were truncated to the collection budget.")
        return {
            "logs": chunks[:20],
            "sources": sources[:20],
            "limitations": list(dict.fromkeys(limitations)),
        }

    async def _collect_codebuild(
        self, build_id: str, secrets: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]] | None:
        if self._codebuild is None or self._cloudwatch is None:
            if not self._settings.aws_region or not self._settings.codebuild_project:
                raise NotConfiguredError("diagnosis AWS log source is not configured")
            config = Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"mode": "standard", "max_attempts": 2},
            )
            self._codebuild = boto3.client(
                "codebuild", region_name=self._settings.aws_region, config=config
            )
            self._cloudwatch = boto3.client(
                "logs", region_name=self._settings.aws_region, config=config
            )
        try:
            response = await asyncio.to_thread(self._codebuild.batch_get_builds, ids=[build_id])
            builds = response.get("builds", [])
            if len(builds) != 1 or builds[0].get("id") != build_id:
                raise ExternalError("CodeBuild returned another build identity")
            build = builds[0]
            if (
                self._settings.codebuild_project
                and build.get("projectName") != self._settings.codebuild_project
            ):
                raise ExternalError("CodeBuild project does not match operator configuration")
            logs = build.get("logs") or {}
            group, stream = logs.get("groupName"), logs.get("streamName")
            if not isinstance(group, str) or not isinstance(stream, str):
                return None
            pages: list[list[dict[str, Any]]] = []
            token: str | None = None
            count = 0
            complete = False
            for _ in range(self._settings.log_max_pages):
                params: dict[str, Any] = {
                    "logGroupName": group,
                    "logStreamName": stream,
                    "startFromHead": False,
                    "limit": 1000,
                }
                if token is not None:
                    params["nextToken"] = token
                page = await asyncio.to_thread(self._cloudwatch.get_log_events, **params)
                events = page.get("events") or []
                pages.insert(0, events)
                count += sum(len(str(event.get("message", "")).encode()) for event in events)
                next_token = page.get("nextBackwardToken")
                if next_token == token or not next_token or not events:
                    complete = True
                    break
                token = next_token
                if count >= self._settings.log_max_bytes:
                    break
            events = [event for page in pages for event in page]
            text = "\n".join(str(event.get("message", "")).rstrip("\n") for event in events)
            text, omitted = _tail(mask_failure_log(text, secrets), self._settings.log_max_bytes)
            if not text.strip():
                return None
            source_id = (
                "cloudwatch-" + hashlib.sha256((group + ":" + stream).encode()).hexdigest()[:24]
            )
            captured_at = None
            if events and type(events[-1].get("timestamp")) is int:
                captured_at = datetime.fromtimestamp(
                    events[-1]["timestamp"] / 1000, UTC
                ).isoformat()
            return (
                {
                    "chunk_id": "codebuild-log",
                    "source_id": source_id,
                    "stage": "build",
                    "stream": "combined",
                    "source_line_start": None,
                    "captured_at": captured_at,
                    "is_complete": (
                        complete
                        and not omitted
                        and build.get("buildStatus")
                        in {"SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"}
                    ),
                    "text": text,
                },
                {
                    "chunk_id": "codebuild-log",
                    "kind": "cloudwatch",
                    "build_id": build_id,
                    "log_group": group,
                    "log_stream": stream,
                },
                []
                if complete
                and not omitted
                and build.get("buildStatus")
                in {"SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"}
                else ["CloudWatch log tail is bounded; earlier lines may be missing."],
            )
        except (BotoCoreError, ClientError):
            raise ExternalError("AWS failure log collection failed") from None
