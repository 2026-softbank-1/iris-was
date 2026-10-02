import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.clients.analysis_source_client import GithubAnalysisSourceClient
from app.clients.source_repository_client import (
    CommitInfo,
    InstallationToken,
    SourceRepositoryClient,
)
from app.core.exceptions import ExternalError, InvalidInputError

SHA = "a" * 40


class SourceClient:
    async def create_installation_token(self, installation_id: int) -> InstallationToken:
        assert installation_id == 42
        return InstallationToken("SECRET_APP_TOKEN", datetime.now(UTC))

    async def find_branch_head(
        self, installation_id: int, full_name: str, branch: str
    ) -> CommitInfo:
        assert (installation_id, full_name, branch) == (42, "team/app", "main")
        return CommitInfo(SHA, "test source")


def archive_bytes(entries: dict[str, bytes], *, special: str | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(f"team-app-{SHA[:7]}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if special is not None:
            member = tarfile.TarInfo(f"team-app-{SHA[:7]}/{special}")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    return output.getvalue()


async def test_source_fixed_revision_private_redirect_and_credentials_excluded(
    tmp_path: Path,
) -> None:
    archive = archive_bytes(
        {
            "package.json": b"{}",
            ".env": b"SECRET=hidden",
            ".env.example": b"TOKEN=example",
            "bin/start": b"node",
        }
    )
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA})
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://codeload.github.com/team/app/legacy.tar.gz/{SHA}?token=signed"
                },
            )
        return httpx.Response(200, content=archive)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GithubAnalysisSourceClient(http, cast(SourceRepositoryClient, SourceClient()))
        assert await client.get_head_sha("https://github.com/team/app", "main", 42) == SHA
        source = await client.fetch_source(
            "https://github.com/team/app", SHA, 42, tmp_path / "source"
        )
    assert (source / "package.json").read_bytes() == b"{}"
    assert not (source / ".env").exists()
    assert (source / ".env.example").is_file()
    assert calls[0].headers["authorization"] == "Bearer SECRET_APP_TOKEN"
    assert "authorization" not in calls[-1].headers


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a\\escape"])
def test_source_archive_rejects_unsafe_entries(tmp_path: Path, name: str) -> None:
    with pytest.raises(InvalidInputError):
        GithubAnalysisSourceClient._unpack(archive_bytes({name: b"x"}), tmp_path / "source", SHA)
    assert not (tmp_path / "escape").exists()


def test_source_archive_rejects_links(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError):
        GithubAnalysisSourceClient._unpack(
            archive_bytes({"package.json": b"{}"}, special="link"), tmp_path / "source", SHA
        )


def test_source_archive_rejects_a_different_revision(tmp_path: Path) -> None:
    with pytest.raises(ExternalError):
        GithubAnalysisSourceClient._unpack(
            archive_bytes({"package.json": b"{}"}), tmp_path / "source", "b" * 40
        )


async def test_source_revision_mismatch_never_downloads_archive(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"sha": "b" * 40})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GithubAnalysisSourceClient(http, cast(SourceRepositoryClient, SourceClient()))
        with pytest.raises(ExternalError) as error:
            await client.fetch_source("team/app", SHA, 42, tmp_path / "source")
    assert len(calls) == 1
    assert "SECRET_APP_TOKEN" not in str(error.value)
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize(
    "target",
    [
        f"https://evil.example/team/app/legacy.tar.gz/{SHA}",
        f"https://codeload.github.com/other/app/legacy.tar.gz/{SHA}",
        f"https://codeload.github.com/team/app/legacy.tar.gz/{'b' * 40}",
        f"https://codeload.github.com/team/app/legacy.tar.gz/{SHA}?redirect=evil",
    ],
)
async def test_source_rejects_foreign_redirect(tmp_path: Path, target: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA})
        return httpx.Response(302, headers={"location": target})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GithubAnalysisSourceClient(http, cast(SourceRepositoryClient, SourceClient()))
        with pytest.raises(ExternalError):
            await client.fetch_source("team/app", SHA, 42, tmp_path / "source")


def test_source_does_not_overwrite_an_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sentinel = source / "user-file"
    sentinel.write_text("preserve")
    with pytest.raises(InvalidInputError):
        GithubAnalysisSourceClient._unpack(archive_bytes({"package.json": b"{}"}), source, SHA)
    assert sentinel.read_text() == "preserve"
