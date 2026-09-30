import os

# Settings 가 DATABASE_URL 을 필수로 요구한다. 테스트는 실제 DB 에 붙지 않는다.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
