"""OpenAPI 명세를 `docs/openapi.json` 으로 내보낸다.

사용: `uv run python -m scripts.export_openapi`
엔드포인트를 추가·변경하면 실행해 결과를 커밋한다. 테스트가 파일이 최신인지 검사한다.
"""

import json
import os
from pathlib import Path

# 명세를 만드는 데 DB 접속은 필요 없다. Settings 가 요구하는 값만 채운다.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://openapi:openapi@127.0.0.1:1/openapi")

from app.main import app  # noqa: E402

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render_openapi() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OPENAPI_PATH.write_text(render_openapi(), encoding="utf-8")
    print(f"wrote {OPENAPI_PATH}")
