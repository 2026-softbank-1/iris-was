import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest

from app.clients.analyzer_client import LocalAnalyzerClient
from app.core.async_io import run_sync
from app.core.exceptions import ExternalError, InvalidInputError, NotConfiguredError

pytest.importorskip("iris_analyzer")
from iris_analyzer.contracts import digest  # noqa: E402
from iris_analyzer.integrations import LocalAnalysisClient  # noqa: E402


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "example",
                "scripts": {"start": "node index.js"},
                "dependencies": {"express": "5.1.0"},
            }
        )
    )
    (root / "index.js").write_text(
        "const express = require('express'); const app = express();\n"
        "app.get('/health', (req, res) => res.send('OK'));\napp.listen(8080, '0.0.0.0');\n"
    )
    return root


async def test_installed_analyzer_preserves_provenance_and_masks_evidence(
    source: Path,
    tmp_path: Path,
) -> None:
    (source / ".env").write_text("DATABASE_URL=SECRET_NOT_FOR_THE_MODEL")
    (source / "config.js").write_text("const apiKey = 'super-secret-value';\n")
    before = (source / "index.js").read_bytes()
    stages: list[str] = []

    async def progress(stage: str) -> None:
        stages.append(stage)

    client = LocalAnalyzerClient(budget_ledger=tmp_path / "ledger.json")
    output = await client.analyze(source, "a" * 40, ".", "static", on_progress=progress)
    result = output["analysisResult"]
    assert output["resultDigest"] == digest(result)
    assert output["verificationReport"]["resultDigest"] == digest(result)
    assert output["verificationReport"]["contextHash"] == output["contextHash"]
    assert output["sourceReadiness"]["sourceSnapshotId"] == output["sourceSnapshotId"]
    assert output["deploymentDossier"]["sourceLink"]["analysisDigest"] == digest(result)
    assert output["deploymentAuthorized"] is False
    assert output["deploymentDossier"]["execution"]["deploymentAuthorized"] is False
    assert output["runReport"]["calls"] == []
    assert "readiness" in stages and "planning" in stages and "validating" in stages
    assert "SECRET_NOT_FOR_THE_MODEL" not in json.dumps(output)
    assert "super-secret-value" not in json.dumps(output)
    assert str(source) not in json.dumps(output)
    assert (source / "index.js").read_bytes() == before
    assert not (tmp_path / "ledger.json").exists()


async def test_analysis_keeps_multiple_service_candidates_and_requested_root_hint(
    source: Path,
    tmp_path: Path,
) -> None:
    nested = source / "api"
    nested.mkdir()
    (nested / "package.json").write_text('{"name":"api","scripts":{"start":"node server.js"}}')
    (nested / "server.js").write_text("require('http').createServer().listen(4000);\n")
    output = await LocalAnalyzerClient(budget_ledger=tmp_path / "ledger").analyze(
        source, "a" * 40, "api", "static"
    )
    assert output["rootDirectory"] == "api"
    assert any(item["root"]["value"] == "api" for item in output["analysisResult"]["services"])


async def test_ai_mode_does_not_silently_fall_back(source: Path, tmp_path: Path) -> None:
    with pytest.raises(NotConfiguredError):
        await LocalAnalyzerClient(budget_ledger=tmp_path / "ledger").analyze(
            source, "a" * 40, ".", "opencode"
        )


@pytest.mark.parametrize("root", ["../", "/etc", "a\\b", "missing"])
async def test_analysis_rejects_invalid_service_root(
    source: Path, tmp_path: Path, root: str
) -> None:
    with pytest.raises(InvalidInputError):
        await LocalAnalyzerClient(budget_ledger=tmp_path / "ledger").analyze(
            source, "a" * 40, root, "static"
        )


async def test_verification_tamper_is_rejected(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = LocalAnalysisClient.analyze_repository

    async def changed(self: object, *args: object, **kwargs: object) -> object:
        outcome = await original(self, *args, **kwargs)
        outcome.run_report["verification"]["resultDigest"] = "0" * 64
        return outcome

    monkeypatch.setattr(LocalAnalysisClient, "analyze_repository", changed)
    with pytest.raises(ExternalError, match="verification"):
        await LocalAnalyzerClient(budget_ledger=tmp_path / "ledger").analyze(
            source, "a" * 40, ".", "static"
        )


async def test_thread_cancellation_waits_for_cleanup() -> None:
    started = threading.Event()
    finish = threading.Event()
    cleaned = threading.Event()

    def operation() -> None:
        started.set()
        finish.wait(timeout=3)
        cleaned.set()

    task = asyncio.create_task(run_sync(operation))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


def test_public_run_report_discards_source_and_credentials() -> None:
    report = {
        "calls": [
            {
                "provider": "hive-ai",
                "model": "glm",
                "apiKey": "secret",
                "rawBody": "source",
                "usage": {"input": 100},
            }
        ],
        "raw": "source",
    }
    safe = LocalAnalyzerClient._public_report(copy.deepcopy(report))
    assert "secret" not in json.dumps(safe) and "source" not in json.dumps(safe)
    assert safe["calls"][0]["usage"] == {"input": 100}
