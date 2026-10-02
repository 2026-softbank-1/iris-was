import pytest

from app.core.exceptions import InvalidInputError
from app.services.repository_url import parse_repository_url


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/iris-org/web",
        "https://github.com/iris-org/web.git",
        "https://github.com/iris-org/web/",
        "https://github.com/iris-org/web/tree/feature/login",
        "http://www.github.com/iris-org/web?tab=readme",
        "git@github.com:iris-org/web.git",
        "iris-org/web",
        "  iris-org/web  ",
    ],
)
def test_parse_repository_url_accepts_common_formats(value: str) -> None:
    assert parse_repository_url(value) == ("iris-org", "web")


def test_parse_repository_url_keeps_dots_in_repository_name() -> None:
    assert parse_repository_url("https://github.com/o/my.app.git") == ("o", "my.app")


@pytest.mark.parametrize(
    "value",
    ["", "web", "https://gitlab.com/iris-org/web", "https://github.com/iris-org", "a/b/c"],
)
def test_parse_repository_url_rejects_other_values(value: str) -> None:
    with pytest.raises(InvalidInputError):
        parse_repository_url(value)
