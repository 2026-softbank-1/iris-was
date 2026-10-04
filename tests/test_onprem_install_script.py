import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "assets" / "onprem" / "install.sh"
REGISTRATION_TOKEN = "test-registration-token-must-not-leak"


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_install_script_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_install_script_shellcheck_reports_nothing() -> None:
    result = subprocess.run(
        ["shellcheck", "-s", "bash", str(SCRIPT_PATH)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout


def test_install_script_never_enables_xtrace() -> None:
    script = SCRIPT_PATH.read_text()

    assert re.search(r"^\s*set\s+-[a-z]*x", script, re.MULTILINE) is None
    assert "bash -x" not in script


def test_install_script_dry_run_prints_every_step_without_token() -> None:
    result = _run_script("--dry-run", "--token", REGISTRATION_TOKEN)

    assert result.returncode == 0, result.stderr
    for step in range(1, 9):
        assert f"[{step}/8]" in result.stdout
    assert "https://api.likelion.uk/api/v1/onprem-servers/bootstrap" in result.stdout
    assert "likelion servers" in result.stdout
    assert "tailscale0 의 tcp 6443·80" in result.stdout
    assert "10.42.0.0/16 10.43.0.0/16" in result.stdout
    assert REGISTRATION_TOKEN not in result.stdout + result.stderr


def test_install_script_dry_run_uses_given_api_url_without_trailing_slash() -> None:
    result = _run_script(
        "--dry-run", f"--token={REGISTRATION_TOKEN}", "--api-url", "https://api.example.test/"
    )

    assert result.returncode == 0, result.stderr
    assert "https://api.example.test/api/v1/onprem-servers/connect" in result.stdout


def test_install_script_without_token_fails() -> None:
    result = _run_script("--dry-run")

    assert result.returncode != 0
    assert "--token" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--dry-run", f"--tokn={REGISTRATION_TOKEN}"),
        ("--dry-run", REGISTRATION_TOKEN),
    ],
)
def test_install_script_unknown_argument_fails_without_echoing_value(args: tuple[str, ...]) -> None:
    result = _run_script(*args)

    assert result.returncode != 0
    assert REGISTRATION_TOKEN not in result.stdout + result.stderr


def test_install_script_invalid_api_url_fails() -> None:
    result = _run_script(
        "--dry-run", "--token", REGISTRATION_TOKEN, "--api-url", "ftp://example.test"
    )

    assert result.returncode != 0
    assert "--api-url" in result.stderr


def _cluster_role_rules() -> str:
    """스크립트에 들어 있는 ClusterRole `iris-onprem-service-deployer` 의 rules 본문."""
    script = SCRIPT_PATH.read_text()
    match = re.search(
        r"kind: ClusterRole\nmetadata:\n  name: iris-onprem-service-deployer\nrules:\n(.*?)\n---",
        script,
        re.DOTALL,
    )
    assert match is not None, "ClusterRole iris-onprem-service-deployer 를 찾지 못했다"
    return match.group(1)


def test_install_script_cluster_role_grants_pod_exec_for_argocd_terminal() -> None:
    # Argo CD 터미널(서비스 콘솔, ADR 0035)이 서버의 Pod 에 exec 한다.
    # WebSocket 은 get, SPDY 는 create 다.
    rules = _cluster_role_rules()

    assert re.search(
        r'- apiGroups: \[""\]\n\s+resources: \[pods/exec\]\n\s+verbs: \[get, create\]', rules
    )


def test_install_script_cluster_role_keeps_existing_deploy_rules() -> None:
    rules = _cluster_role_rules()

    for expected in ("rollouts", "sealedsecrets", "networkpolicies", "deployments, replicasets"):
        assert expected in rules


def test_install_script_rbac_only_dry_run_needs_no_token_and_skips_install_steps() -> None:
    result = _run_script("--rbac-only", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "--rbac-only" in result.stdout
    assert "iris-onprem-service-deployer" in result.stdout
    # 등록·설치 단계는 하나도 실행하지 않는다.
    assert "[1/8]" not in result.stdout
    assert "bootstrap" not in result.stdout


def test_install_script_rbac_only_rejects_token_without_echoing_it() -> None:
    result = _run_script("--rbac-only", f"--token={REGISTRATION_TOKEN}")

    assert result.returncode != 0
    assert "--token" in result.stderr
    assert REGISTRATION_TOKEN not in result.stdout + result.stderr


def test_install_script_without_rbac_only_still_requires_token() -> None:
    result = _run_script("--dry-run")

    assert result.returncode != 0
    assert "--token" in result.stderr


def test_install_script_rbac_only_applies_the_same_manifest_as_install() -> None:
    script = SCRIPT_PATH.read_text()

    # 두 경로가 같은 함수(kubectl apply)를 써야 규칙이 어긋나지 않는다.
    for function in ("grant_deploy_access", "update_rbac"):
        body = re.search(rf"^{function}\(\) \{{\n(.*?)^\}}", script, re.DOTALL | re.MULTILINE)
        assert body is not None, function
        assert "apply_deploy_access" in body.group(1), function
