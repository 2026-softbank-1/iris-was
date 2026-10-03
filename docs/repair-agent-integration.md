# Repair candidate integration

운영 주소, WAS 세션 인증, GitHub 쓰기 토큰 및 코드수정 API 계약은 [코드수정 운영 API 명세](repair-api.md)를 참고한다.

The WAS hands the internal repair agent the original persisted `diagnosis-result.v3`, selected remediation IDs, and an exact source snapshot from the failed deployment's build. The public diagnosis response is a view model and must not be used as the original diagnosis. Ownership is checked by the orchestration service before the handoff service receives its records.

The request uses `iris.repair-request.v1`; its `requestId` also becomes the `Idempotency-Key` header. Request and artifact calls use `X-API-Key`. Persist the request identity and pinned metadata before the model call. A source URL refresh may change the signed URL but must preserve every semantic field and both hashes. Never resolve a repair against the latest source branch.

`RepairHandoffService.prepare_request` validates the deployment/diagnosis relationship, failure state, successful raw diagnosis, selected plans, source commit/root and policy. It computes an archive SHA256 and canonical source manifest SHA256 without running source code. The source client requires an exact configured HTTPS host, port 443, no credentials, and refuses redirects. Parsing is bounded to 10 MiB compressed, 20 MiB file contents, 5 MiB per file, 1,000 files and 2,000 total entries. It rejects traversal, duplicate paths, symlinks, special files and file/directory collisions, and removes one common GitHub archive wrapper. The manifest is the sorted array of `path`, `sha256`, `mode` (`100644` or `100755`) and byte `size`, encoded as compact sorted-key UTF-8 JSON.

`HttpRepairAgentClient.submit` sends once. After a timeout it looks up the persistent receipt once, recovering a completed result or returning an existing pending receipt. It never repeats a model POST automatically. Failed lookup/uncertain transport becomes `UNKNOWN_OUTCOME`, which is not automatically retryable. A subsequent receipt lookup is read-only and may safely reconcile the durable WAS record.

A completed `candidate_ready` result is a proposed patch. The handoff requires `validation.status=not_run`, `validation.owner=was`, and false publishing/deployment authorization. Configuration-only and insufficient-evidence outcomes are completed generation outcomes too; they are not successful code verification. Artifact names are restricted to `patch.diff`, `changes.json` and `manifest.json`. Downloads use constructed internal paths, ignoring provider-supplied URLs, and verify the recorded SHA256 and byte length before returning bytes.

The agent owns generation. WAS still owns isolated verification, candidate reconstruction/digest verification, source branch/PR publication and deployment tracking. This candidate integration does not perform these later steps.

## Verification

Run `uv run pytest tests/test_repair_agent_client.py tests/test_repair_handoff_service.py` for timeout recovery without duplicate POSTs, redirect refusal, exact source host restrictions, source traversal/symlink/duplicate/collision rejection, canonical manifest compatibility, frozen commit preservation and artifact transport integrity. Run `uv run mypy app` and `uv run ruff check .` for repository checks.

A live E2E should start the real fix-agent with local ignored credentials, then call the native WAS repair route against a dedicated test DB and a failed deployment fixture. Keep its raw diagnosis and source SHA bound to the same frozen archive. A controlled source transport is acceptable for a local provider-boundary test, but document that S3 IAM, real HTTPS snapshot retrieval and deployment are not covered. Existing deployment-worker E2E tests exercise GitOps/Argo CD, not repair generation, and must not substitute for this test.
