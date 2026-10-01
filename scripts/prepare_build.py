"""Run only worker source preparation, without a queue, CodeBuild or deployment."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from app.clients.analyzer_build_client import SubprocessAnalyzerBuildClient
from app.core.build_preparation_config import BuildPreparationSettings
from app.core.exceptions import AppError, InvalidInputError
from app.schemas.build_preparation import PrepareBuildRequest
from app.services.build_preparation_service import BuildPreparationService


async def prepare_build(request_path: Path, command: list[str], timeout_seconds: float) -> int:
    try:
        request_bytes = await asyncio.to_thread(request_path.read_bytes)
        if len(request_bytes) > 65536:
            raise InvalidInputError("build preparation request exceeds the size limit")
        request = PrepareBuildRequest.model_validate_json(request_bytes)
        client = (
            SubprocessAnalyzerBuildClient(command, timeout_seconds=timeout_seconds)
            if command
            else None
        )
        result = await BuildPreparationService(client).prepare_build(request)
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        return 0 if result.status == "ready" else 2
    except AppError as error:
        sys.stdout.write(
            json.dumps({"error": {"code": error.code, "message": error.message}}) + "\n"
        )
        return 2
    except (ValidationError, OSError):
        sys.stdout.write(json.dumps({"error": {"code": "INVALID_INPUT"}}) + "\n")
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--analyzer-command-json", help="JSON argv array for an installed analyzer")
    arguments = parser.parse_args()
    settings = BuildPreparationSettings()
    command = settings.analyzer_command
    if arguments.analyzer_command_json is not None:
        value = json.loads(arguments.analyzer_command_json)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            parser.error("--analyzer-command-json must contain a JSON array of strings")
        command = value
    raise SystemExit(
        asyncio.run(prepare_build(arguments.request, command, settings.timeout_seconds))
    )


if __name__ == "__main__":
    main()
