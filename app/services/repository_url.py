"""사용자가 붙여넣은 저장소 주소를 `owner/name` 으로 바꾼다."""

import re

from app.core.exceptions import InvalidInputError

_OWNER = r"(?P<owner>[\w.-]+)"
_NAME = r"(?P<name>[\w.-]+?)(?:\.git)?"

# 받는 형식: https://github.com/owner/repo (.git, /tree/<branch> 등 뒤에 붙어도 됨),
# git@github.com:owner/repo.git, owner/repo
_PATTERNS = (
    re.compile(rf"^https?://(?:www\.)?github\.com/{_OWNER}/{_NAME}(?:[/?#].*)?$"),
    re.compile(rf"^git@github\.com:{_OWNER}/{_NAME}$"),
    re.compile(rf"^{_OWNER}/{_NAME}$"),
)


def parse_repository_url(value: str) -> tuple[str, str]:
    stripped = value.strip()
    for pattern in _PATTERNS:
        match = pattern.match(stripped)
        if match is not None:
            return match.group("owner"), match.group("name")
    raise InvalidInputError("not a github repository url")
