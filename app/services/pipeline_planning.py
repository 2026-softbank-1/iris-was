"""Resolve validated source observations and user bindings before asking the analyzer to plan."""

import copy
from typing import Any

from app.core.exceptions import InvalidInputError
from app.models.pipeline_run import PipelineRun
from app.models.service_analysis import ServiceAnalysis
from app.services.pipeline_contract import digest, validate_pipeline_plan


def _observed(field: Any) -> Any:
    if isinstance(field, dict) and field.get("status") == "detected":
        return field.get("value")
    return None


def _question(key: str, reason: str, kind: str = "user_configuration") -> dict[str, str]:
    return {"key": key, "reason": reason, "kind": kind}


def resolve_pipeline_inputs(
    run: PipelineRun, analysis: ServiceAnalysis
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    result = analysis.analysis_result or {}
    readiness = analysis.source_readiness or {}
    supplied = run.confirmed_inputs or {}
    saved = run.config_snapshot or {}
    questions: list[dict[str, str]] = []
    candidates = [
        candidate for candidate in result.get("services", []) if isinstance(candidate, dict)
    ]
    if run.root_directory != ".":
        candidates = [
            candidate
            for candidate in candidates
            if _observed(candidate.get("root")) == run.root_directory
            or run.root_directory in candidate.get("componentRoots", [])
        ]
    selected_id = supplied.get("service_candidate_id")
    selected = (
        next((item for item in candidates if item.get("serviceId") == selected_id), None)
        if selected_id
        else None
    )
    if selected_id and selected is None:
        raise InvalidInputError("selected candidate is not part of the fixed analysis")
    if selected is None and len(candidates) == 1:
        selected = candidates[0]
    if selected is None:
        questions.append(
            _question(
                "serviceCandidateId",
                "Choose one application from the analyzed deployment candidates.",
            )
        )
        return {}, questions
    root = _observed(selected.get("root"))
    if not isinstance(root, str):
        questions.append(
            _question(
                "serviceCandidateId",
                "The application root is not verified by source evidence.",
                "code_review",
            )
        )
        return {}, questions
    target_rows = [
        row
        for row in readiness.get("buildTargets", [])
        if isinstance(row, dict) and row.get("contextPath") == root
    ]
    docker_paths = {
        row["dockerfilePath"] for row in target_rows if isinstance(row.get("dockerfilePath"), str)
    }
    builder = (
        supplied.get("builder")
        or saved.get("builder")
        or ("dockerfile" if docker_paths else "railpack")
    )
    path = supplied.get("dockerfile_path") or saved.get("dockerfile_path")
    if builder == "dockerfile" and not path:
        if len(docker_paths) == 1:
            full = next(iter(docker_paths))
            path = full[len(root) + 1 :] if root != "." and full.startswith(root + "/") else full
        else:
            questions.append(
                _question(
                    "dockerfilePath", "Choose the Dockerfile inside the selected service root."
                )
            )
    port = supplied.get("port", saved.get("port"))
    if port is None:
        observed = {
            _observed(field)
            for field in selected.get("ports", [])
            if field.get("scope") == "container" and isinstance(_observed(field), int)
        }
        if len(observed) == 1:
            port = next(iter(observed))
        else:
            questions.append(
                _question("port", "Confirm the application's container listening port.")
            )
    role = _observed(selected.get("role"))
    runtime = _observed(selected.get("runtime"))
    if runtime is None or role is None:
        questions.append(
            _question("runtime", "Runtime or application role needs source review.", "code_review")
        )
    build = supplied.get("build_command", saved.get("build_command"))
    if build is None:
        build = _observed(selected.get("buildCommand"))
    if build == "none":
        build = None
    start = supplied.get("start_command", saved.get("start_command"))
    if start is None:
        start = _observed(selected.get("startCommand"))
    # Missing image CMD is a real execution failure handled by the deployment/log diagnosis path.
    variables = {
        item["key"]: copy.deepcopy(item)
        for item in supplied.get("variables", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    if isinstance(port, int) and "PORT" not in variables:
        variables["PORT"] = {"key": "PORT", "value": str(port)}
    elif isinstance(port, int) and variables["PORT"].get("value") != str(port):
        questions.append(
            _question("variables.PORT", "PORT must match the confirmed listening port.")
        )
    components = set(selected.get("componentRoots", [])) | {root}
    for variable in readiness.get("environmentVariables", []):
        if (
            not isinstance(variable, dict)
            or variable.get("required") is False
            or variable.get("condition")
        ):
            continue
        if variable.get("phase") not in {"runtime", "build"}:
            continue
        component = variable.get("component")
        if component not in components and not any(
            isinstance(component, str) and c != "." and component.startswith(c + "/")
            for c in components
        ):
            continue
        key = variable.get("key")
        if (
            variable.get("phase") == "build"
            and isinstance(key, str)
            and key in variables
            and "value" not in variables[key]
        ):
            questions.append(
                _question(
                    "buildSecret." + key,
                    "A runtime Kubernetes Secret cannot supply a build-time secret. "
                    "Configure a supported build secret source before building.",
                    "code_review",
                )
            )
        if isinstance(key, str) and key not in variables:
            questions.append(
                _question(
                    "variables." + key,
                    "Provide the required "
                    + variable["phase"]
                    + " binding for "
                    + key
                    + ". Sensitive values use an existing Secret reference.",
                )
            )
    issues = [
        item
        for item in (analysis.verification_report or {}).get("reviewFindings", [])
        if isinstance(item, dict) and item.get("blocking") is True
    ]
    for issue in issues:
        questions.append(
            _question(
                "sourceReview." + str(issue.get("reasonCode", issue.get("key", "finding"))),
                str(issue.get("reason", "Resolve the source finding before building.")),
                "code_review",
            )
        )
    for finding in readiness.get("findings", []):
        if isinstance(finding, dict) and finding.get("severity") == "error":
            questions.append(
                _question(
                    "sourceReadiness." + str(finding.get("ruleId", "error")),
                    str(
                        finding.get("reason", "Resolve the source readiness error before building.")
                    ),
                    "code_review",
                )
            )
    config = {
        "builder": builder,
        "dockerfile_path": path if builder == "dockerfile" else None,
        "platform": saved.get("platform") or "linux/amd64",
        "root_directory": root,
        "port": port,
        "build_command": build,
        "start_command": start,
        "railpack_version": saved.get("railpack_version") or "0.40.1"
        if builder == "railpack"
        else None,
        "runtime_env": list(variables.values()),
        "pipeline_run_id": run.id,
        "analysis_plan_digest": None,
    }
    return {
        "candidate": selected,
        "config": config,
        "variables": list(variables.values()),
    }, questions


def unresolved_planning_questions(dossier: dict[str, Any]) -> list[dict[str, str]]:
    """Defer image/rollout checks while retaining source and unsupported binding blockers."""
    questions = []
    for item in dossier.get("deploymentPlan", {}).get("questions", []):
        if not isinstance(item, dict) or item.get("requiredForExecution") is not True:
            continue
        key = str(item.get("id", ""))
        if key in {"adapter", "workloads", "readiness"} or key.startswith(
            ("environment-owner-", "bind-", "volume-", "secret-", "environment-")
        ):
            questions.append(
                _question(
                    "planning." + key,
                    str(item.get("reason", "Resolve the required planning input.")),
                    "code_review",
                )
            )
    return questions


def planning_request(
    run: PipelineRun, resolved: dict[str, Any], targets: list[dict[str, Any]]
) -> dict[str, Any]:
    candidate_id = resolved["candidate"]["serviceId"]
    config = resolved["config"]
    environment = []
    secrets = []
    for variable in resolved["variables"]:
        if "value" in variable:
            environment.append(
                {
                    "serviceId": candidate_id,
                    "environmentKey": variable["key"],
                    "value": variable["value"],
                }
            )
        else:
            secrets.append(
                {
                    "serviceId": candidate_id,
                    "environmentKey": variable["key"],
                    "name": variable["secret_ref"],
                    "key": variable["secret_key"],
                }
            )
    region = next((target["region"] for target in targets if target.get("region")), None)
    return {
        "schemaVersion": "iris.planning-request.v1",
        "target": {
            "stack": "existing_kubernetes",
            "cloud": "aws",
            "region": region,
            "architecture": "arm64" if config["platform"] == "linux/arm64" else "x86_64",
            "environment": "production",
        },
        "bindings": {
            "namespace": f"svc-{run.service_id}",
            "runtimeEnv": environment,
            "secretRefs": secrets,
        },
        "constraints": {"availability": "single_az"},
    }


def make_pipeline_plan(
    run: PipelineRun,
    analysis: ServiceAnalysis,
    resolved: dict[str, Any],
    targets: list[dict[str, Any]],
    dossier: dict[str, Any],
) -> dict[str, Any]:
    source_link = dossier.get("sourceLink", {})
    if (
        source_link.get("analysisDigest") != analysis.result_digest
        or source_link.get("sourceSnapshotId") != analysis.source_snapshot_id
    ):
        raise InvalidInputError("analyzer planning output changed the fixed source")
    plan = {
        "schemaVersion": "iris.pipeline-plan.v1",
        "pipelineRunId": run.id,
        "serviceId": run.service_id,
        "sourceSha": run.source_sha,
        "sourceRepositoryUrl": run.source_repository_url,
        "sourceSnapshotId": analysis.source_snapshot_id,
        "analysisResultDigest": analysis.result_digest,
        "selectedServiceCandidateId": resolved["candidate"]["serviceId"],
        "buildConfig": resolved["config"],
        "targetIds": run.target_ids,
        "targetBindings": targets,
        "analyzerPlanDigest": dossier["deploymentPlan"]["planDigest"],
        "userInputsDigest": digest(run.confirmed_inputs),
        "authorization": "user_requested_pipeline",
        "executionAuthorized": False,
    }
    validate_pipeline_plan(plan)
    return plan
