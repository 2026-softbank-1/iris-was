#!/usr/bin/env bash
# 로컬 개발 DB(PostgreSQL 18)를 Docker(OrbStack)로 띄우고 .env 의 DATABASE_URL 을 맞춘다.
# 사용: scripts/dev-db.sh          # 켜기(이미 켜져 있으면 그대로 둔다)
#       scripts/dev-db.sh stop     # 끄기(데이터는 남는다)
#       scripts/dev-db.sh reset    # 데이터까지 지우고 새로 만든다
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.dev.yml)
ENV_FILE=.env

case "${1:-up}" in
  stop) "${COMPOSE[@]}" stop; exit 0 ;;
  reset) "${COMPOSE[@]}" down -v ;;
  up) ;;
  *) echo "usage: $0 [up|stop|reset]" >&2; exit 2 ;;
esac

command -v docker >/dev/null || { echo "docker 가 없다. OrbStack 을 설치·실행한다." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker 엔진에 연결할 수 없다. OrbStack 을 실행한다." >&2; exit 1; }

umask 077
touch "$ENV_FILE"

# 비밀번호는 한 번만 만들고 .env 에 둔다(출력하지 않는다).
if ! grep -q '^POSTGRES_PASSWORD=' "$ENV_FILE"; then
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> "$ENV_FILE"
fi
password="$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)"

# DATABASE_URL 을 이 DB 에 맞게 갱신한다.
tmp="$(mktemp)"
grep -v '^DATABASE_URL=' "$ENV_FILE" > "$tmp" || true
printf 'DATABASE_URL=postgresql+asyncpg://iris:%s@127.0.0.1:5432/softbank_iris\n' "$password" >> "$tmp"
mv "$tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

"${COMPOSE[@]}" up -d --wait
echo "DB 준비 완료: 127.0.0.1:5432/softbank_iris (사용자 iris, 비밀번호는 .env)"
echo "다음: uv run alembic upgrade head"
