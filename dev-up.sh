#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  printf 'Error: Docker is not installed. Install Docker Desktop and try again.\n' >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  printf 'Error: Docker Compose is unavailable. Install or update Docker Desktop.\n' >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  printf 'Error: Docker is not running. Start Docker Desktop and try again.\n' >&2
  exit 1
fi

# Compose reads .env automatically. A shell-provided SECRET_KEY takes
# precedence, which is useful in CI or for a temporary local session.
if [[ -z "${SECRET_KEY:-}" ]]; then
  if [[ ! -f .env ]]; then
    printf 'Error: .env is missing. Copy .env.example to .env and set SECRET_KEY.\n' >&2
    exit 1
  fi

  if ! grep -Eq '^SECRET_KEY=.+$' .env || grep -Eq '^SECRET_KEY=your-secret-key-here$' .env; then
    printf 'Error: set a non-placeholder SECRET_KEY in .env.\n' >&2
    exit 1
  fi
fi

printf 'Starting Apsara Assistant development stack...\n'
printf '  API:      http://localhost:8000\n'
printf '  API docs: http://localhost:8000/docs\n'
printf 'PostgreSQL data is kept in the apsara-db Docker volume.\n\n'

# docker-compose.yml waits for Postgres, applies Alembic migrations, then
# starts the API and the queue worker. Extra arguments are forwarded, so
# `./dev-up.sh -d` starts the same stack in the background.
exec docker compose up --build "$@"
