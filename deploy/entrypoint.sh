#!/usr/bin/env bash
# Container entrypoint: apply DB migrations, then hand off to the server (CMD).
set -euo pipefail

echo "[entrypoint] ENVIRONMENT=${ENVIRONMENT:-unset}"
echo "[entrypoint] Running database migrations..."
alembic upgrade head

echo "[entrypoint] Starting: $*"
exec "$@"
