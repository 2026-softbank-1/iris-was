import pytest

from app.core.exceptions import InvalidInputError
from app.services.raw_variables import parse_raw_variables


def test_parse_raw_variables_plain_lines_returns_values() -> None:
    raw = "DATABASE_URL=postgres://u:p@h/db\nPORT_RANGE = 1-2\n"

    assert parse_raw_variables(raw) == {"DATABASE_URL": "postgres://u:p@h/db", "PORT_RANGE": "1-2"}


def test_parse_raw_variables_skips_blank_lines_and_comments() -> None:
    raw = "\n# 주석\n  \nA=1\n   # 들여쓴 주석\r\nB=2\r\n"

    assert parse_raw_variables(raw) == {"A": "1", "B": "2"}


def test_parse_raw_variables_export_prefix_and_empty_value() -> None:
    assert parse_raw_variables("export A=1\nB=\nC=''\n") == {"A": "1", "B": "", "C": ""}


def test_parse_raw_variables_double_quoted_follows_json_escapes() -> None:
    # 웹 Raw 편집기는 값을 JSON.stringify 해서 내보낸다.
    raw = 'A="a \\"b\\" \\\\ c"\nKEY="-----BEGIN-----\\nabc\\n-----END-----"\nU="\\uD55C\\uAE00"\n'

    assert parse_raw_variables(raw) == {
        "A": 'a "b" \\ c',
        "KEY": "-----BEGIN-----\nabc\n-----END-----",
        "U": "한글",
    }


def test_parse_raw_variables_single_quoted_is_literal() -> None:
    assert parse_raw_variables(r"A='a\nb # not comment'") == {"A": r"a\nb # not comment"}


def test_parse_raw_variables_inline_comment_only_after_whitespace() -> None:
    raw = 'A=value # comment\nB=a#b\nC="x # y" # tail\n'

    assert parse_raw_variables(raw) == {"A": "value", "B": "a#b", "C": "x # y"}


def test_parse_raw_variables_duplicate_key_last_wins() -> None:
    assert parse_raw_variables("A=1\nA=2\n") == {"A": "2"}


def test_parse_raw_variables_empty_text_returns_empty() -> None:
    assert parse_raw_variables("") == {}
    assert parse_raw_variables("\n# only comment\n") == {}


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ("A=1\nnot a variable\n", 2),
        ("1A=x\n", 1),
        ("=x\n", 1),
        ('A="unterminated\n', 1),
        ('A="bad \\q escape"\n', 1),
        ("A='unterminated\n", 1),
        ('A="x" trailing\n', 1),
    ],
)
def test_parse_raw_variables_invalid_line_raises_with_line_number(raw: str, line: int) -> None:
    with pytest.raises(InvalidInputError) as caught:
        parse_raw_variables(raw)

    assert caught.value.fields == {"field": "raw", "line": line}
