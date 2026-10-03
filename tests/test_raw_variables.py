import pytest

from app.core.exceptions import InvalidInputError
from app.services.raw_variables import parse_raw_entries, parse_raw_variables


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


PEM = "-----BEGIN PRIVATE KEY-----\nMIIFAKE\nabc==\n-----END PRIVATE KEY-----"


def test_parse_raw_variables_double_quoted_multiline_value_keeps_line_breaks() -> None:
    raw = f'BEFORE=1\nPRIVATE_KEY="{PEM}"\nAFTER=2\n'

    assert parse_raw_variables(raw) == {"BEFORE": "1", "PRIVATE_KEY": PEM, "AFTER": "2"}


def test_parse_raw_variables_multiline_value_with_crlf_uses_line_feed() -> None:
    raw = f'KEY="{PEM}"\nA=1\n'.replace("\n", "\r\n")

    assert parse_raw_variables(raw) == {"KEY": PEM, "A": "1"}


def test_parse_raw_variables_single_quoted_multiline_value_is_literal() -> None:
    raw = "KEY='line1\n  \\n # kept\nline3'\nA=1\n"

    assert parse_raw_variables(raw) == {"KEY": "line1\n  \\n # kept\nline3", "A": "1"}


def test_parse_raw_variables_multiline_value_allows_escapes_and_comment_after_close() -> None:
    raw = 'KEY="a \\"b\\"\nsecond" # tail\nA=1\n'

    assert parse_raw_variables(raw) == {"KEY": 'a "b"\nsecond', "A": "1"}


def test_parse_raw_variables_hash_and_equal_lines_inside_quotes_are_not_parsed() -> None:
    raw = 'KEY="x\n# not a comment\nB=2\n"\nA=1\n'

    assert parse_raw_variables(raw) == {"KEY": "x\n# not a comment\nB=2\n", "A": "1"}


def test_parse_raw_entries_reports_the_line_each_value_starts_on() -> None:
    raw = f'A=1\n\n# c\nKEY="{PEM}"\nB=2\nA=3\n'

    entries = parse_raw_entries(raw)

    assert {key: entry.line for key, entry in entries.items()} == {"A": 9, "KEY": 4, "B": 8}
    assert entries["A"].value == "3"


def test_parse_raw_variables_unclosed_multiline_quote_reports_line_it_opened_on() -> None:
    raw = 'A=1\nKEY="-----BEGIN-----\nabc\nB=2\n'

    with pytest.raises(InvalidInputError) as caught:
        parse_raw_variables(raw)

    assert caught.value.fields == {"field": "raw", "line": 2}
    assert [(i.field, i.reason) for i in caught.value.issues] == [
        ("raw", "line 2: quoted value is not closed")
    ]


def test_parse_raw_variables_invalid_line_reports_issue_for_response_details() -> None:
    with pytest.raises(InvalidInputError) as caught:
        parse_raw_variables("A=1\n\nnot a variable\n")

    assert caught.value.message == "invalid variable line"
    assert [(i.field, i.reason) for i in caught.value.issues] == [
        ("raw", "line 3: invalid variable line")
    ]
