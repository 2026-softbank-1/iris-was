import pytest

from app.core.exceptions import BuildFailedError
from app.enums import Builder, FailureCode
from app.models import Service
from app.services.builder_detection import detect_builder, parse_iris_config


def _service(builder: Builder | None = None, dockerfile_path: str | None = None) -> Service:
    return Service(builder=builder, dockerfile_path=dockerfile_path)


@pytest.mark.parametrize(
    ("files", "config", "service", "expected_builder", "expected_path"),
    [
        ({"Dockerfile"}, None, _service(), Builder.DOCKERFILE, "Dockerfile"),
        ({"package.json"}, None, _service(), Builder.RAILPACK, "Dockerfile"),
        ({"Dockerfile"}, None, _service(Builder.RAILPACK), Builder.RAILPACK, "Dockerfile"),
        (
            {"docker/App"},
            None,
            _service(dockerfile_path="./docker/App"),
            Builder.DOCKERFILE,
            "docker/App",
        ),
        (
            {"Dockerfile", "web.Dockerfile"},
            b'{"build": {"builder": "dockerfile", "dockerfilePath": "web.Dockerfile"}}',
            _service(Builder.RAILPACK, "Dockerfile"),
            Builder.DOCKERFILE,
            "web.Dockerfile",
        ),
        (
            {"Dockerfile"},
            b'{"build": {"builder": "railpack"}}',
            _service(),
            Builder.RAILPACK,
            "Dockerfile",
        ),
    ],
)
def test_detect_builder_priority_selects_expected_builder(
    files: set[str],
    config: bytes | None,
    service: Service,
    expected_builder: Builder,
    expected_path: str,
) -> None:
    plan = detect_builder(files, parse_iris_config(config) if config else None, service)

    assert (plan.builder, plan.dockerfile_path) == (expected_builder, expected_path)


@pytest.mark.parametrize(
    ("config", "service"),
    [
        (None, _service(Builder.DOCKERFILE)),
        (b'{"build": {"builder": "dockerfile"}}', _service()),
    ],
)
def test_detect_builder_dockerfile_missing_raises_config_required(
    config: bytes | None, service: Service
) -> None:
    with pytest.raises(BuildFailedError) as exc_info:
        detect_builder({"main.py"}, parse_iris_config(config) if config else None, service)

    assert exc_info.value.failure_code == FailureCode.BUILD_CONFIG_REQUIRED


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b'{"build": {"builder": "nixpacks"}}',
        b"[]",
        b'{"deploy": {"preDeployCommand": "alembic upgrade head"}}',
        b'{"deploy": {"healthcheckPath": "health"}}',
        b'{"deploy": {"healthcheckTimeout": 29}}',
        b'{"deploy": {"startCommand": "node \'main.js"}}',
        b'{"deploy": {"startCommand": "   "}}',
    ],
)
def test_parse_iris_config_invalid_raises_config_required(content: bytes) -> None:
    with pytest.raises(BuildFailedError) as exc_info:
        parse_iris_config(content)

    assert exc_info.value.failure_code == FailureCode.BUILD_CONFIG_REQUIRED


def test_detect_builder_railpack_passes_commands_and_deploy_config() -> None:
    config = parse_iris_config(
        b'{"build": {"buildCommand": "npm run build"},'
        b' "deploy": {"startCommand": "node dist/main.js", "healthcheckPath": "/health"}}'
    )

    plan = detect_builder({"package.json"}, config, _service())

    assert plan.railpack_env == {
        "RAILPACK_BUILD_CMD": "npm run build",
        "RAILPACK_START_CMD": "node dist/main.js",
    }
    assert plan.deploy_config == {"startCommand": "node dist/main.js", "healthcheckPath": "/health"}


def test_detect_builder_without_config_uses_service_commands_from_analysis() -> None:
    service = Service(build_command="npm run build", start_command="node dist/main.js")

    plan = detect_builder({"package.json"}, None, service)

    assert plan.railpack_env == {
        "RAILPACK_BUILD_CMD": "npm run build",
        "RAILPACK_START_CMD": "node dist/main.js",
    }
    assert plan.deploy_config == {"startCommand": "node dist/main.js"}


def test_detect_builder_config_commands_win_over_service_commands() -> None:
    config = parse_iris_config(
        b'{"build": {"buildCommand": "make"}, "deploy": {"startCommand": "./run"}}'
    )
    service = Service(build_command="npm run build", start_command="node dist/main.js")

    plan = detect_builder({"package.json"}, config, service)

    assert plan.railpack_env == {"RAILPACK_BUILD_CMD": "make", "RAILPACK_START_CMD": "./run"}
    assert plan.deploy_config == {"startCommand": "./run"}


def test_detect_builder_invalid_service_start_command_raises_config_required() -> None:
    service = Service(start_command="node 'main.js")

    with pytest.raises(BuildFailedError) as exc_info:
        detect_builder({"package.json"}, None, service)

    assert exc_info.value.failure_code == FailureCode.BUILD_CONFIG_REQUIRED
