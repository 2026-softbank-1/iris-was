"""Build·Deploy Worker 가 쓰는 AWS Client. boto3 는 동기라 asyncio.to_thread 로 감싼다.

AWS 호출 실패는 모두 ExternalError(재시도) 로 바꾼다. 쓰로틀링 재시도는 botocore 가 먼저 한다.
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.async_io import run_sync
from app.core.exceptions import ExternalError

_BOTO_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, signature_version="s3v4")
PRESIGNED_URL_SECONDS = 15 * 60

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


class CodeBuildClient:
    def __init__(self, region: str, project_name: str) -> None:
        self._client = boto3.client("codebuild", region_name=region, config=_BOTO_CONFIG)
        self._project_name = project_name

    async def start_build(
        self,
        env: dict[str, str],
        idempotency_token: str,
        timeout_minutes: int,
        buildspec: str | None = None,
    ) -> str:
        overrides = {"buildspecOverride": buildspec} if buildspec is not None else {}
        response = await _call(
            self._client.start_build,
            projectName=self._project_name,
            environmentVariablesOverride=[
                {"name": name, "value": value, "type": "PLAINTEXT"} for name, value in env.items()
            ],
            idempotencyToken=idempotency_token,
            timeoutInMinutesOverride=timeout_minutes,
            **overrides,
        )
        build_id: str = response["build"]["id"]
        return build_id

    async def find_build_for_request(self, request_id: str) -> str | None:
        """Recover a StartBuild whose response was lost; never blindly submit another build."""
        next_token: str | None = None
        for _ in range(5):
            arguments: dict[str, Any] = {
                "projectName": self._project_name,
                "sortOrder": "DESCENDING",
            }
            if next_token is not None:
                arguments["nextToken"] = next_token
            page = await _call(self._client.list_builds_for_project, **arguments)
            build_ids = page.get("ids", [])
            if build_ids:
                result = await _call(self._client.batch_get_builds, ids=build_ids)
                matches = [
                    build["id"]
                    for build in result.get("builds", [])
                    if any(
                        value.get("name") == "IRIS_BUILD_ID" and value.get("value") == request_id
                        for value in build.get("environment", {}).get("environmentVariables", [])
                    )
                ]
                if len(matches) > 1:
                    raise ExternalError("multiple codebuild executions found for the build")
                if matches:
                    return str(matches[0])
            next_token = page.get("nextToken")
            if next_token is None:
                return None
        raise ExternalError("codebuild recovery search exceeded the bounded history")

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
        return CodeBuildResult(
            status=build["buildStatus"],
            failed_phase=failed_phase,
            log_url=build.get("logs", {}).get("deepLink"),
        )

    async def stop_build(self, build_id: str) -> None:
        await _call(self._client.stop_build, id=build_id)


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
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ExternalError("ecr returned an invalid image digest")
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


class ArtifactStore:
    """빌드 입력(소스 스냅샷)을 S3 에 둔다.

    버킷 기본 암호화(SSE-KMS)·1일 lifecycle 은 인프라가 건다.
    """

    def __init__(self, region: str, bucket: str) -> None:
        # presigned URL 이 글로벌 엔드포인트로 만들어지면 리전 서명과 맞지 않아 거절된다.
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=_BOTO_CONFIG,
        )
        self._bucket = bucket

    async def put_snapshot(self, build_id: int, path: Path, digest: str | None = None) -> str:
        key = f"snapshots/{build_id}/{digest}.tar.gz" if digest else f"snapshots/{build_id}.tar.gz"
        # A cancelled worker must keep the file until the synchronous upload has ended.
        await run_sync(
            lambda: self._client.upload_file(Filename=str(path), Bucket=self._bucket, Key=key)
        )
        return key

    async def presign(self, key: str) -> str:
        url: str = await _call(
            self._client.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=PRESIGNED_URL_SECONDS,
        )
        return url
