import pytest

from app.core.exceptions import (
    InvalidInputError,
    ServiceNotFoundError,
    VariableConflictError,
    VariableDecryptionError,
    VariableNotFoundError,
)
from app.models.service import Service
from app.services.variable_service import (
    MAX_VALUE_LENGTH,
    MAX_VARIABLES,
    build_system_variables,
)
from tests.fakes_variable import OWNER, STRANGER, VariableSetup


@pytest.fixture
async def setup() -> VariableSetup:
    return await VariableSetup().build()


async def test_create_variable_stores_encrypted_value_and_commits(setup: VariableSetup) -> None:
    entry = await setup.variable_service().create_variable(
        OWNER, setup.service.id, "DATABASE_URL", "postgres://secret"
    )

    assert (entry.key, entry.value) == ("DATABASE_URL", "postgres://secret")
    [stored] = setup.variables.variables
    assert "secret" not in stored.encrypted_value
    assert setup.cipher.decrypt(stored.encrypted_value) == "postgres://secret"
    assert setup.session.commit_count == 1


async def test_create_variable_empty_value_is_allowed(setup: VariableSetup) -> None:
    entry = await setup.variable_service().create_variable(OWNER, setup.service.id, "EMPTY", "")

    assert entry.value == ""


async def test_create_variable_duplicate_key_raises_conflict(setup: VariableSetup) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "A", "1")

    with pytest.raises(VariableConflictError):
        await service.create_variable(OWNER, setup.service.id, "A", "2")

    assert len(setup.variables.variables) == 1


@pytest.mark.parametrize(
    "key", ["", "1A", "A-B", "A B", "한글", "a" * 129, "PORT", "IRIS_ANYTHING", "IRIS_"]
)
async def test_create_variable_invalid_or_reserved_key_raises_invalid_input(
    setup: VariableSetup, key: str
) -> None:
    with pytest.raises(InvalidInputError):
        await setup.variable_service().create_variable(OWNER, setup.service.id, key, "v")

    assert setup.variables.variables == []


async def test_create_variable_lowercase_key_is_kept_as_is(setup: VariableSetup) -> None:
    entry = await setup.variable_service().create_variable(OWNER, setup.service.id, "node_env", "x")

    assert entry.key == "node_env"


async def test_create_variable_value_too_long_raises_invalid_input(setup: VariableSetup) -> None:
    with pytest.raises(InvalidInputError):
        await setup.variable_service().create_variable(
            OWNER, setup.service.id, "BIG", "x" * (MAX_VALUE_LENGTH + 1)
        )


async def test_create_variable_over_limit_raises_invalid_input(setup: VariableSetup) -> None:
    service = setup.variable_service()
    for i in range(MAX_VARIABLES):
        await service.create_variable(OWNER, setup.service.id, f"K{i}", "v")

    with pytest.raises(InvalidInputError):
        await service.create_variable(OWNER, setup.service.id, "ONE_MORE", "v")


async def test_create_variable_not_owner_raises_service_not_found(setup: VariableSetup) -> None:
    with pytest.raises(ServiceNotFoundError):
        await setup.variable_service().create_variable(STRANGER, setup.service.id, "A", "1")

    assert setup.variables.variables == []


async def test_search_variables_returns_sorted_plaintext_and_system_variables(
    setup: VariableSetup,
) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "B", "2")
    await service.create_variable(OWNER, setup.service.id, "A", "1")

    result = await service.search_variables(OWNER, setup.service.id)

    assert [(v.key, v.value) for v in result.variables] == [("A", "1"), ("B", "2")]
    system = {v.key: v.value for v in result.system_variables}
    assert system["PORT"] == "8080"
    assert system["IRIS_SERVICE_NAME"] == "web"
    assert {"IRIS_TARGET_NAME", "IRIS_DEPLOYMENT_ID"} <= system.keys()


async def test_search_variables_only_own_service_variables(setup: VariableSetup) -> None:
    other = await setup.services.save(
        Service(
            project_id=setup.service.project_id,
            name="api",
            source_repository_url="https://github.com/iris-org/api",
            github_installation_id=1,
            source_branch="main",
            is_auto_deploy=True,
        )
    )
    service = setup.variable_service()
    await service.create_variable(OWNER, other.id, "OTHER", "x")

    result = await service.search_variables(OWNER, setup.service.id)

    assert result.variables == []


async def test_search_variables_not_owner_raises_service_not_found(setup: VariableSetup) -> None:
    with pytest.raises(ServiceNotFoundError):
        await setup.variable_service().search_variables(STRANGER, setup.service.id)


async def test_search_variables_wrong_key_raises_decryption_error(setup: VariableSetup) -> None:
    await setup.variable_service().create_variable(OWNER, setup.service.id, "A", "1")
    rotated = VariableSetup()
    rotated.services, rotated.variables = setup.services, setup.variables

    with pytest.raises(VariableDecryptionError):
        await rotated.variable_service().search_variables(OWNER, setup.service.id)


async def test_update_variable_replaces_value(setup: VariableSetup) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "A", "old")

    entry = await service.update_variable(OWNER, setup.service.id, "A", "new")

    assert entry.value == "new"
    assert setup.cipher.decrypt(setup.variables.variables[0].encrypted_value) == "new"


async def test_update_variable_missing_raises_not_found(setup: VariableSetup) -> None:
    with pytest.raises(VariableNotFoundError):
        await setup.variable_service().update_variable(OWNER, setup.service.id, "NOPE", "x")


async def test_delete_variable_removes_it(setup: VariableSetup) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "A", "1")

    await service.delete_variable(OWNER, setup.service.id, "A")

    assert setup.variables.variables == []


async def test_delete_variable_missing_raises_not_found(setup: VariableSetup) -> None:
    with pytest.raises(VariableNotFoundError):
        await setup.variable_service().delete_variable(OWNER, setup.service.id, "NOPE")


async def test_replace_variables_replaces_whole_set(setup: VariableSetup) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "KEEP", "old")
    await service.create_variable(OWNER, setup.service.id, "DROP", "x")

    result = await service.replace_variables(
        OWNER, setup.service.id, 'KEEP="new"\nADDED=1\n# comment\n'
    )

    assert [(v.key, v.value) for v in result.variables] == [("ADDED", "1"), ("KEEP", "new")]
    stored = {v.key: setup.cipher.decrypt(v.encrypted_value) for v in setup.variables.variables}
    assert stored == {"KEEP": "new", "ADDED": "1"}


async def test_replace_variables_empty_text_clears_all(setup: VariableSetup) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "A", "1")

    result = await service.replace_variables(OWNER, setup.service.id, "")

    assert result.variables == []
    assert setup.variables.variables == []


async def test_replace_variables_reserved_key_keeps_existing_untouched(
    setup: VariableSetup,
) -> None:
    service = setup.variable_service()
    await service.create_variable(OWNER, setup.service.id, "A", "1")

    with pytest.raises(InvalidInputError):
        await service.replace_variables(OWNER, setup.service.id, "A=2\nPORT=3000\n")

    assert setup.cipher.decrypt(setup.variables.variables[0].encrypted_value) == "1"


async def test_replace_variables_invalid_line_raises_invalid_input(setup: VariableSetup) -> None:
    with pytest.raises(InvalidInputError):
        await setup.variable_service().replace_variables(OWNER, setup.service.id, "oops")


async def test_replace_variables_over_limit_raises_invalid_input(setup: VariableSetup) -> None:
    raw = "\n".join(f"K{i}=v" for i in range(MAX_VARIABLES + 1))

    with pytest.raises(InvalidInputError):
        await setup.variable_service().replace_variables(OWNER, setup.service.id, raw)


async def test_replace_variables_not_owner_raises_service_not_found(setup: VariableSetup) -> None:
    with pytest.raises(ServiceNotFoundError):
        await setup.variable_service().replace_variables(STRANGER, setup.service.id, "A=1")


def test_build_system_variables_lists_platform_injected_names() -> None:
    service = Service(name="web")

    keys = [v.key for v in build_system_variables(service)]

    assert keys == [
        "PORT",
        "IRIS_SERVICE_NAME",
        "IRIS_TARGET_NAME",
        "IRIS_DEPLOYMENT_ID",
        "IRIS_PUBLIC_DOMAIN",
        "IRIS_GIT_COMMIT_SHA",
    ]
