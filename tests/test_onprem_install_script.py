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
    assert "CPU·메모리(metrics-server)를 플랫폼에 보낸다" in result.stdout
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
