"""Build·Deploy Worker 가 쓰는 AWS Client. boto3 는 동기라 asyncio.to_thread 로 감싼다.

AWS 호출 실패는 모두 ExternalError(재시도) 로 바꾼다. 쓰로틀링 재시도는 botocore 가 먼저 한다.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import boto3
from boto3.exceptions import RetriesExceededError, S3TransferFailedError, S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.exceptions import ExternalError, InvalidInputError, NotFoundError

_BOTO_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, signature_version="s3v4")
PRESIGNED_URL_SECONDS = 15 * 60
_MISSING_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# 배포된 이미지(r-*)는 최근 5개를 먼저 지킨다. 상위 규칙에 걸린 이미지는 하위 규칙이 지우지 못한다.
# untagged 는 7일 뒤 지우고, 빌드 이미지(b-*)는 최근 10개만 남긴다.
_ECR_LIFECYCLE_POLICY = json.dumps(
    {
        "rules": [
            {
                "rulePriority": 1,
                "selection": {
                    "tagStatus": "tagged",
                    "tagPatternList": ["r-*"],
                    "countType": "imageCountMoreThan",
                    "countNumber": 5,
                },
                "action": {"type": "expire"},
            },
            {
                "rulePriority": 2,
                "selection": {
                    "tagStatus": "untagged",
                    "countType": "sinceImagePushed",
                    "countUnit": "days",
                    "countNumber": 7,
                },
                "action": {"type": "expire"},
            },
            {
                "rulePriority": 3,
                "selection": {
                    "tagStatus": "tagged",
                    "tagPatternList": ["b-*"],
                    "countType": "imageCountMoreThan",
                    "countNumber": 10,
                },
                "action": {"type": "expire"},
            },
        ]
    }
)


async def _call(fn: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, **kwargs)
    except (BotoCoreError, ClientError, S3UploadFailedError) as exc:
        raise ExternalError("aws request failed", operation=fn.__name__) from exc


@dataclass(frozen=True)
class CodeBuildResult:
    status: str  # IN_PROGRESS · SUCCEEDED · FAILED · FAULT · TIMED_OUT · STOPPED
    failed_phase: str | None
    log_url: str | None
    # CloudWatch Logs 에서 이 빌드의 로그를 찾는 위치. 아직 로그를 쓰기 전이면 없다.
    log_group: str | None = None
    log_stream: str | None = None


class CodeBuildClient:
    def __init__(self, region: str, project_name: str) -> None:
        self._client = boto3.client("codebuild", region_name=region, config=_BOTO_CONFIG)
        self._project_name = project_name

    async def start_build(
        self, env: dict[str, str], idempotency_token: str, timeout_minutes: int
    ) -> str:
        response = await _call(
            self._client.start_build,
            projectName=self._project_name,
            environmentVariablesOverride=[
                {"name": name, "value": value, "type": "PLAINTEXT"} for name, value in env.items()
            ],
            idempotencyToken=idempotency_token,
            timeoutInMinutesOverride=timeout_minutes,
        )
        build_id: str = response["build"]["id"]
        return build_id

    async def get_build(self, build_id: str) -> CodeBuildResult:
        response = await _call(self._client.batch_get_builds, ids=[build_id])
        if not response["builds"]:
            raise ExternalError("codebuild build not found", codebuild_build_id=build_id)
        build = response["builds"][0]
        failed_phase = next(
            (
                phase["phaseType"]
                for phase in build.get("phases", [])
                if phase.get("phaseStatus") not in (None, "SUCCEEDED", "IN_PROGRESS")
            ),
            None,
        )
        logs = build.get("logs", {})
        return CodeBuildResult(
            status=build["buildStatus"],
            failed_phase=failed_phase,
            log_url=logs.get("deepLink"),
            log_group=logs.get("groupName"),
            log_stream=logs.get("streamName"),
        )

    async def stop_build(self, build_id: str) -> None:
        await _call(self._client.stop_build, id=build_id)


@dataclass(frozen=True)
class LogLine:
    timestamp_ms: int
    message: str


@dataclass(frozen=True)
class BuildLogTail:
    """빌드 로그의 끝부분. 오래된 줄부터 순서대로이고, 앞부분이 잘렸으면 is_truncated 다."""

    lines: list[LogLine]
    is_truncated: bool


class BuildLogClient(Protocol):
    async def fetch_tail(self, log_group: str, log_stream: str, max_lines: int) -> BuildLogTail:
        """빌드 로그의 마지막 max_lines 줄. 읽지 못하면 ExternalError 다."""
        ...


@dataclass(frozen=True)
class BuildLogChunk:
    """빌드 로그를 앞에서부터 읽은 한 덩어리. next_token 으로 이어 읽는다."""

    lines: list[LogLine]
    # 새 줄이 없으면 보낸 토큰이 그대로 돌아온다. 그래서 진행 중인 빌드를 같은 토큰으로 폴링한다.
    next_token: str | None


class BuildLogReader(Protocol):
    """Control API 가 배포 상세 화면에 빌드 로그를 보여 주려고 쓰는 읽기 전용 조회."""

    async def read_events(
        self, log_group: str, log_stream: str, limit: int, next_token: str | None
    ) -> BuildLogChunk:
        """처음(또는 next_token)부터 시간순으로 limit 줄. 스트림이 아직 없으면 빈 덩어리다."""
        ...


class CloudWatchBuildLogClient:
    """CodeBuild 가 CloudWatch Logs 에 쓴 빌드 로그를 읽는다(`logs:GetLogEvents` 권한 필요)."""

    def __init__(self, region: str, client: Any | None = None) -> None:
        self._client = client or boto3.client("logs", region_name=region, config=_BOTO_CONFIG)

    @classmethod
    def create_reader(cls, region: str) -> "CloudWatchBuildLogClient":
        """Control API 용. S3 서명 설정이 없는 기본 설정에 짧은 타임아웃을 건다."""
        config = Config(
            retries={"mode": "standard", "max_attempts": 3}, connect_timeout=3, read_timeout=10
        )
        return cls(region, boto3.client("logs", region_name=region, config=config))

    async def read_events(
        self, log_group: str, log_stream: str, limit: int, next_token: str | None
    ) -> BuildLogChunk:
        params: dict[str, Any] = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startFromHead": True,
            "limit": limit,
        }
        if next_token is not None:
            params["nextToken"] = next_token
        try:
            response = await asyncio.to_thread(self._client.get_log_events, **params)
        except self._client.exceptions.ResourceNotFoundException:
            # 빌드를 막 시작해 CodeBuild 가 스트림을 아직 만들지 않았다.
            return BuildLogChunk([], next_token)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidParameterException" and next_token:
                raise InvalidInputError("cursor is invalid") from exc
            raise ExternalError("aws request failed", operation="get_log_events") from exc
        except BotoCoreError as exc:
            raise ExternalError("aws request failed", operation="get_log_events") from exc
        # CodeBuild 는 이벤트마다 줄바꿈을 붙여 보낸다.
        lines = [
            LogLine(
                timestamp_ms=int(event["timestamp"]), message=str(event["message"]).rstrip("\r\n")
            )
            for event in response.get("events", [])
        ]
        return BuildLogChunk(lines, response.get("nextForwardToken"))

    async def fetch_tail(self, log_group: str, log_stream: str, max_lines: int) -> BuildLogTail:
        response = await _call(
            self._client.get_log_events,
            logGroupName=log_group,
            logStreamName=log_stream,
            limit=max_lines,
            startFromHead=False,
        )
        lines = [
            LogLine(timestamp_ms=int(event["timestamp"]), message=str(event["message"]))
            for event in response.get("events", [])
        ]
        # 가져온 줄이 한도와 같으면 앞에 더 있을 수 있다.
        return BuildLogTail(lines=lines, is_truncated=len(lines) >= max_lines)


class EcrClient:
    def __init__(self, region: str) -> None:
        self._client = boto3.client("ecr", region_name=region, config=_BOTO_CONFIG)

    async def ensure_repository(self, service_id: int) -> str:
        """iris/services/{service_id} 저장소를 없으면 만들고 URI 를 돌려준다."""
        name = f"iris/services/{service_id}"
        try:
            response = await asyncio.to_thread(
                self._client.create_repository,
                repositoryName=name,
                # 빌드 태그는 덮어쓸 수 없게 하고, 레지스트리 캐시 태그만 예외로 둔다.
                imageTagMutability="IMMUTABLE_WITH_EXCLUSION",
                imageTagMutabilityExclusionFilters=[{"filterType": "WILDCARD", "filter": "cache"}],
            )
            uri: str = response["repository"]["repositoryUri"]
        except self._client.exceptions.RepositoryAlreadyExistsException:
            response = await _call(self._client.describe_repositories, repositoryNames=[name])
            uri = response["repositories"][0]["repositoryUri"]
        except (BotoCoreError, ClientError) as exc:
            raise ExternalError("aws request failed", operation="create_repository") from exc
        # 생성 직후 죽었을 수도 있어 매번 건다. 같은 정책이면 바뀌는 것이 없다.
        await _call(
            self._client.put_lifecycle_policy,
            repositoryName=name,
            lifecyclePolicyText=_ECR_LIFECYCLE_POLICY,
        )
        return uri

    async def get_image_digest(self, repository_name: str, image_tag: str) -> str:
        response = await _call(
            self._client.describe_images,
            repositoryName=repository_name,
            imageIds=[{"imageTag": image_tag}],
        )
        digest: str = response["imageDetails"][0]["imageDigest"]
        return digest

    async def tag_image(self, repository_name: str, image_digest: str, image_tag: str) -> None:
        """digest 에 태그를 하나 더 붙인다. 이미 붙어 있으면 통과한다."""
        response = await _call(
            self._client.batch_get_image,
            repositoryName=repository_name,
            imageIds=[{"imageDigest": image_digest}],
        )
        if not response["images"]:
            raise ExternalError("ecr image not found", image_digest=image_digest)
        image = response["images"][0]
        try:
            await asyncio.to_thread(
                self._client.put_image,
                repositoryName=repository_name,
                imageManifest=image["imageManifest"],
                imageManifestMediaType=image["imageManifestMediaType"],
                imageDigest=image_digest,
                imageTag=image_tag,
            )
        except self._client.exceptions.ImageAlreadyExistsException:
            pass
        except (BotoCoreError, ClientError) as exc:
            raise ExternalError("aws request failed", operation="put_image") from exc


class SnapshotUrlClient(Protocol):
    async def presign_snapshot(self, build_id: int) -> str:
        """빌드의 소스 스냅샷을 내려받는 단기 URL."""
        ...


class UploadWriter(Protocol):
    """Control API 가 쓴다. `likelion up` 이 올린 아카이브를 저장소에 둔다."""

    async def put_upload(self, public_id: str, path: Path) -> str:
        """`path` 의 파일을 올리고 저장 키를 돌려준다. 저장하지 못하면 ExternalError 다."""
        ...


class UploadReader(Protocol):
    """Build Worker 가 쓴다. 저장된 업로드 아카이브를 내려받는다."""

    async def download_upload(self, storage_key: str, dest: Path) -> None:
        """없으면 NotFoundError, 그 밖의 실패는 ExternalError 다."""
        ...


class ArtifactStore:
    """빌드 입력(소스 스냅샷)과 사용자가 올린 소스 아카이브를 S3 에 둔다.

    버킷 기본 암호화(SSE-S3)·1일 lifecycle 은 인프라가 건다. 업로드(`uploads/`)와 스냅샷
    (`snapshots/`)은 접두어로 나뉘어, 컴포넌트마다 필요한 접두어만 IAM 으로 허용한다.
    """

    def __init__(self, region: str, bucket: str, client: Any | None = None) -> None:
        # presigned URL 이 글로벌 엔드포인트로 만들어지면 리전 서명과 맞지 않아 거절된다.
        self._client = client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=_BOTO_CONFIG,
        )
        self._bucket = bucket

    @staticmethod
    def snapshot_key(build_id: int) -> str:
        return f"snapshots/{build_id}.tar.gz"

    @staticmethod
    def upload_key(public_id: str) -> str:
        return f"uploads/{public_id}.tar.gz"

    async def put_snapshot(self, build_id: int, path: Path) -> str:
        key = self.snapshot_key(build_id)
        await _call(self._client.upload_file, Filename=str(path), Bucket=self._bucket, Key=key)
        return key

    async def put_upload(self, public_id: str, path: Path) -> str:
        key = self.upload_key(public_id)
        await _call(
            self._client.upload_file,
            Filename=str(path),
            Bucket=self._bucket,
            Key=key,
            ExtraArgs={"ContentType": "application/gzip"},
        )
        return key

    async def download_upload(self, storage_key: str, dest: Path) -> None:
        try:
            await asyncio.to_thread(
                self._client.download_file,
                Bucket=self._bucket,
                Key=storage_key,
                Filename=str(dest),
            )
        except ClientError as exc:
            # 키가 없으면 HeadObject 가 404 다. 권한이 없어도 403 이라 구분이 안 되니 재시도한다.
            if exc.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
                raise NotFoundError("upload object not found") from exc
            raise ExternalError("aws request failed", operation="download_file") from exc
        except (BotoCoreError, S3TransferFailedError, RetriesExceededError) as exc:
            raise ExternalError("aws request failed", operation="download_file") from exc

    async def presign_snapshot(self, build_id: int) -> str:
        """빌드가 올린 소스 스냅샷을 내려받는 단기 URL. 읽기 권한만 쓴다."""
        return await self.presign(self.snapshot_key(build_id))

    async def presign(self, key: str) -> str:
        url: str = await _call(
            self._client.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=PRESIGNED_URL_SECONDS,
        )
        return url
