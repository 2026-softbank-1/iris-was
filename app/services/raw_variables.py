"""Raw 편집기 텍스트(`KEY=VALUE` 한 줄에 하나, .env 형식)를 변수 집합으로 읽는다.

웹 Raw 편집기가 `KEY="값"`(JSON 문자열) 형태로 내보내므로 큰따옴표 값은 JSON 이스케이프를 따른다.
"""

import json
import re

from app.core.exceptions import InvalidInputError

_LINE_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_DOUBLE_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
_SINGLE_QUOTED = re.compile(r"^'([^']*)'\s*(?:#.*)?$")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse_raw_variables(raw: str) -> dict[str, str]:
    """빈 줄과 `#` 로 시작하는 줄은 건너뛴다. 같은 키가 또 나오면 뒤의 값을 쓴다.

    형식이 맞지 않는 줄은 몇 번째 줄인지 알려 주며 거부한다.
    """
    variables: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = _LINE_PATTERN.match(text)
        value = _parse_value(match.group(2)) if match else None
        if match is None or value is None:
            raise InvalidInputError("invalid variable line", field="raw", line=line_number)
        variables[match.group(1)] = value
    return variables


def _parse_value(text: str) -> str | None:
    """따옴표가 닫히지 않았거나 이스케이프가 잘못되면 None 이다."""
    text = text.strip()
    if text.startswith('"'):
        quoted = _DOUBLE_QUOTED.match(text)
        if quoted is None:
            return None
        try:
            decoded = json.loads(f'"{quoted.group(1)}"', strict=False)
        except json.JSONDecodeError:
            return None
        assert isinstance(decoded, str)
        return decoded
    if text.startswith("'"):
        quoted = _SINGLE_QUOTED.match(text)
        return quoted.group(1) if quoted else None
    return _INLINE_COMMENT.sub("", text).strip()
