from pathlib import Path

from app.core.exceptions import NotConfiguredError


class UnavailableAnalysisSourceClient:
    """Keep persisted analysis reads available when GitHub credentials are absent."""

    async def get_head_sha(self, repository_url: str, branch: str, installation_id: int) -> str:
        raise NotConfiguredError("github app is not configured")

    async def fetch_source(
        self, repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path:
        raise NotConfiguredError("github app is not configured")
