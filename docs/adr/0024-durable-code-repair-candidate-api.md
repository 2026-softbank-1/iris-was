# 0024 — Durable code repair candidate API

- Status: Accepted
- Date: 2026-10-03

## Context

A successful original error diagnosis can contain code remediation plans. The repair agent generates a candidate against a pinned source snapshot, but generation alone does not validate or deploy that candidate. Synchronous model waiting exceeds the public load balancer request window.

## Decision

The authenticated Control API creates `deployment_repairs`, commits a `RUNNING` row and immediately responds with 202. A background task uses a new session to generate once. The API accepts only `diagnosisId`, `planIds` and an `Idempotency-Key`; it resolves service ownership, the paired failed deployment, successful raw `diagnosis-result.v3`, and the retained source snapshot itself. It freezes source SHA, repository, service root, diagnosis, selected plans and server policy. Source hashes and the exact agent input digest are committed before model submission. Signed download URLs remain in memory.

There is one `RUNNING` repair per deployment and one client key per service. An atomic generation claim prevents multiple background submissions. The agent's ID is `was-repair-{id}`. A repeated key returns its persisted attempt and never starts another call; changed diagnosis/deployment/plans conflict. Stale or uncertain requests recover by reading that exact agent receipt, never by reposting. Unknown outcomes are explicit, so callers can choose a new client key knowingly.

GET responses retain candidate generation status, `validation.status=not_run`, and false publication/deployment authorization. Artifact URLs route through authenticated service ownership checks; artifact bytes are checked against stored SHA-256 and byte length. Existing failed deployment state is unchanged.

## Consequences and limits

Configuration uses `REPAIR_AGENT_URL`, `REPAIR_AGENT_API_KEY`, timeout, deadline, cost and exact source-host allowlist settings, plus existing read-only snapshot access. BackgroundTasks follows the existing diagnosis pattern, rather than adding a new jobs enum or worker. A process crash leaves a durable attempt that can be recovered by GET; it does not automatically resume or spend again. Source snapshots older than the conservative 23-hour retention window cannot start new generation.

This implementation does not run isolated candidate verification, publish source branches/PRs, or deploy to production. A future publisher needs separate WAS worker ownership, fresh repository permission checks, base-commit/preimage verification, isolated validation, explicit user authorization and reviewable PR publication. `candidate_ready` cannot be treated as a successful deployment.
