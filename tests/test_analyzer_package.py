"""Bundled wheel provenance and installed package identity are part of the integration contract."""

import hashlib
import json
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest


def test_bundled_analyzer_matches_manifest_and_installed_package() -> None:
    pytest.importorskip("iris_analyzer")
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "vendor/analyzer-manifest.json").read_text())
    wheel = root / "vendor" / manifest["wheel"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest["sha256"]
    assert manifest["commit"] == "1d9d2e38086b394d60fe90d889c6978357e35681"
    with zipfile.ZipFile(wheel) as archive:
        included = {name for name in archive.namelist() if name.startswith("iris_analyzer/")}
        assert included == set(manifest["packageFiles"])
        for name, expected in manifest["packageFiles"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
            installed = files("iris_analyzer").joinpath(name.removeprefix("iris_analyzer/"))
            assert hashlib.sha256(installed.read_bytes()).hexdigest() == expected
