#!/usr/bin/env bash
# Команда запуску для Azure App Service (Linux, Python). Один процес: SQLite не любить кількох записувачів.
set -e
mkdir -p "$(dirname "${DB_PATH:-/home/data/web.db}")"
exec python -m uvicorn productcart.web.app:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*'
