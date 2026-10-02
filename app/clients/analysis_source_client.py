"""Fetch fixed GitHub source as bounded data; never run checkout hooks."""

import asyncio
import io
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.clients.source_repository_client import SourceRepositoryClient
from app.core.exceptions import ExternalError, InvalidInputError, RepositoryNotAccessibleError
from app.services.repository_url import parse_repository_url

_SHA = re.compile(r"^[a-f0-9]{40}$")
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_UNPACKED_BYTES = 100 * 1024 * 1024
_MAX_ENTRIES = 60000
_MAX_FILES = 20000
_MAX_FILE_BYTES = 64 * 1024 * 1024
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        ".aws",
        ".ssh",
        ".kube",
        ".azure",
        "secrets",
        ".secrets",
        "credentials",
        "__pycache__",
    }
)
_CREDENTIAL_FILES = frozenset(
    {".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519", "credentials.json"}
)


class AnalysisSourceClient(Protocol):
    async def get_head_sha(self, repository_url: str, branch: str, installation_id: int) -> str: ...

    async def fetch_source(
        self, repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path: ...


def _repository_name(value: str) -> str:
    owner, repository = parse_repository_url(value)
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
        for part in (owner, repository)
    ):
        raise InvalidInputError("invalid source repository")
    return f"{owner}/{repository}"


def _is_excluded(path: PurePosixPath) -> bool:
    name = path.name.lower()
    is_example = name in {".env.example", ".env.sample", ".env.template"} or (
        name.startswith(".env.") and name.endswith((".example", ".sample", ".template"))
    )
    return (
        any(part.lower() in _EXCLUDED_DIRECTORIES for part in path.parts)
        or name == ".env"
        or (name.startswith(".env.") and not is_example)
        or name in _CREDENTIAL_FILES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


class GithubAnalysisSourceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        source_client: SourceRepositoryClient,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._http = http
        self._source_client = source_client
        self._api_base_url = api_base_url.rstrip("/")

    async def get_head_sha(self, repository_url: str, branch: str, installation_id: int) -> str:
        commit = await self._source_client.find_branch_head(
            installation_id, _repository_name(repository_url), branch
        )
        if commit is None:
            raise RepositoryNotAccessibleError("source branch is unavailable")
        if not _SHA.fullmatch(commit.sha):
            raise ExternalError("github returned an invalid source revision")
        return commit.sha

    async def fetch_source(
        self, repository_url: str, source_sha: str, installation_id: int, destination: Path
    ) -> Path:
        if not _SHA.fullmatch(source_sha):
            raise InvalidInputError("analysis requires a full source revision")
        name = _repository_name(repository_url)
        token = (await self._source_client.create_installation_token(installation_id)).token
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            revision = await self._http.get(
                f"{self._api_base_url}/repos/{name}/commits/{source_sha}",
                headers=headers,
                follow_redirects=False,
            )
            self._check_status(revision)
            if revision.json().get("sha") != source_sha:
                raise ExternalError("github source revision does not match the request")
            archive = await self._download(
                f"{self._api_base_url}/repos/{name}/tarball/{source_sha}", headers, name, source_sha
            )
        except httpx.HTTPError:
            raise ExternalError("github source download failed") from None
        except (ValueError, TypeError, AttributeError):
            raise ExternalError("github returned an invalid source revision") from None
        # Cancellation must wait for extraction before the worker removes its temporary directory.
        task = asyncio.create_task(
            asyncio.to_thread(self._unpack, archive, destination, source_sha)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    async def _download(
        self, url: str, headers: dict[str, str], name: str, source_sha: str
    ) -> bytes:
        async with self._http.stream(
            "GET", url, headers=headers, follow_redirects=False
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                parsed = urlsplit(location)
                expected = f"/{name}/legacy.tar.gz/{source_sha}"
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "codeload.github.com"
                    or parsed.port is not None
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path.casefold() != expected.casefold()
                    or any(key != "token" or not value for key, value in parse_qsl(parsed.query))
                    or parsed.fragment
                ):
                    raise ExternalError("github returned an invalid source redirect")
            else:
                self._check_status(response)
                return await self._read_archive(response)
        # The archive redirect carries GitHub's own authorization. Never forward the App token.
        async with self._http.stream("GET", location, follow_redirects=False) as response:
            self._check_status(response)
            return await self._read_archive(response)

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403, 404}:
            raise RepositoryNotAccessibleError("github source is inaccessible")
        if response.status_code != 200:
            raise ExternalError("github source request failed", status_code=response.status_code)

    @staticmethod
    async def _read_archive(response: httpx.Response) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > _MAX_ARCHIVE_BYTES:
                raise InvalidInputError("source archive exceeds the analysis size limit")
        return bytes(data)

    @staticmethod
    def _unpack(archive: bytes, destination: Path, source_sha: str) -> Path:
        if not destination.is_absolute() or destination.exists():
            raise InvalidInputError("analysis source requires a new absolute directory")
        if destination.parent.resolve() != destination.parent:
            raise InvalidInputError("analysis source cannot follow directory links")
        destination.mkdir(mode=0o700)
        seen: set[str] = set()
        prefix: str | None = None
        unpacked_bytes = 0
        file_count = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
                for count, member in enumerate(stream, start=1):
                    path = PurePosixPath(member.name)
                    unpacked_bytes += member.size
                    if (
                        count > _MAX_ENTRIES
                        or unpacked_bytes > _MAX_UNPACKED_BYTES
                        or path.is_absolute()
                        or ".." in path.parts
                        or "\\" in member.name
                        or not path.parts
                        or any(ord(char) < 32 for char in member.name)
                        or member.name.rstrip("/") != path.as_posix()
                        or path.as_posix() in seen
                        or not (member.isfile() or member.isdir())
                    ):
                        raise InvalidInputError("source archive contains invalid entries")
                    seen.add(path.as_posix())
                    if prefix is None:
                        prefix = path.parts[0]
                        if not prefix.endswith("-" + source_sha[:7]):
                            raise ExternalError("source archive revision prefix does not match")
                    if path.parts[0] != prefix:
                        raise InvalidInputError("source archive contains multiple roots")
                    if len(path.parts) == 1:
                        if not member.isdir():
                            raise InvalidInputError("source archive root must be a directory")
                        continue
                    relative = PurePosixPath(*path.parts[1:])
                    if _is_excluded(relative) or member.isdir():
                        continue
                    if member.size > _MAX_FILE_BYTES:
                        raise InvalidInputError("source file exceeds the analysis size limit")
                    file_count += 1
                    if file_count > _MAX_FILES:
                        raise InvalidInputError("source file count exceeds the analysis limit")
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = stream.extractfile(member)
                    if content is None:
                        raise InvalidInputError("source archive entry is unreadable")
                    with target.open("xb") as output:
                        while chunk := content.read(65536):
                            output.write(chunk)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
        except (tarfile.TarError, EOFError, OSError):
            raise InvalidInputError("source archive cannot be safely unpacked") from None
        if file_count == 0:
            raise InvalidInputError("source archive contains no analyzable files")
        return destination
