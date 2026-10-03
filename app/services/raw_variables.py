"""Raw 편집기 텍스트(`KEY=VALUE` 한 줄에 하나, .env 형식)를 변수 집합으로 읽는다.

웹 Raw 편집기가 `KEY="값"`(JSON 문자열) 형태로 내보내므로 큰따옴표 값은 JSON 이스케이프를 따른다.
따옴표 값은 닫는 따옴표가 나올 때까지 여러 줄에 걸칠 수 있다(PEM 개인키 등).
"""

import json
import re
from dataclasses import dataclass

from app.core.exceptions import FieldIssue, InvalidInputError

_LINE_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_TRAILING = re.compile(r"^\s*(?:#.*)?$")
_INLINE_COMMENT = re.compile(r"\s+#.*$")
_QUOTES = ('"', "'")


@dataclass(frozen=True)
class RawVariable:
    """`line` 은 값이 시작하는 줄(1부터)이다. 같은 키가 또 나오면 뒤쪽 줄이다."""

    line: int
    value: str


def parse_raw_variables(raw: str) -> dict[str, str]:
    return {key: variable.value for key, variable in parse_raw_entries(raw).items()}


def parse_raw_entries(raw: str) -> dict[str, RawVariable]:
    """빈 줄과 `#` 로 시작하는 줄은 건너뛴다. 같은 키가 또 나오면 뒤의 값을 쓴다.

    형식이 맞지 않는 곳을 만나면 몇 번째 줄인지 알려 주며 거부한다. 따옴표가 닫히지 않으면 뒤의 줄이
    어디까지 그 값인지 알 수 없으므로 첫 오류에서 멈춘다.
    """
    entries: dict[str, RawVariable] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        text = lines[index].lstrip()
        index += 1
        if not text.strip() or text.startswith("#"):
            continue
        match = _LINE_PATTERN.match(text)
        if match is None:
            raise _invalid_line(line_number, "invalid variable line")
        value_text = match.group(2)
        if value_text.lstrip().startswith(_QUOTES):
            value_text = value_text.lstrip()
            end = _find_closing_quote(value_text)
            while end is None and index < len(lines):
                value_text += "\n" + lines[index]
                index += 1
                end = _find_closing_quote(value_text)
            if end is None:
                raise _invalid_line(line_number, "quoted value is not closed")
            value = _decode_quoted(value_text, end)
            if value is None:
                raise _invalid_line(line_number, "invalid variable line")
        else:
            value = _INLINE_COMMENT.sub("", value_text).strip()
        entries[match.group(1)] = RawVariable(line_number, value)
    return entries


def _invalid_line(line_number: int, reason: str) -> InvalidInputError:
    return InvalidInputError(
        "invalid variable line",
        issues=[FieldIssue("raw", f"line {line_number}: {reason}")],
        field="raw",
        line=line_number,
    )


def _find_closing_quote(text: str) -> int | None:
    """text 첫 글자(여는 따옴표)의 짝을 찾는다. 큰따옴표 값의 `\\"` 는 건너뛴다."""
    quote = text[0]
    position = 1
    while position < len(text):
        char = text[position]
        if quote == '"' and char == "\\":
            position += 2
            continue
        if char == quote:
            return position
        position += 1
    return None


def _decode_quoted(text: str, end: int) -> str | None:
    """닫는 따옴표 뒤에 주석이 아닌 글이 있거나 이스케이프가 잘못되면 None 이다."""
    if _TRAILING.match(text[end + 1 :]) is None:
        return None
    body = text[1:end]
    if text[0] == "'":
        return body
    try:
        # strict=False: 여러 줄 값의 실제 줄바꿈 문자를 허용한다.
        decoded = json.loads(f'"{body}"', strict=False)
    except json.JSONDecodeError:
        return None
    assert isinstance(decoded, str)
    return decoded
