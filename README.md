# Apsara Assistant — Backend API

FastAPI + PostgreSQL backend for the Apsara AI sales assistant.

## Requirements

- Python 3.12+
- PostgreSQL 16 (or Docker)
- An OpenAI API key (for chat/auto-reply)
- A Cloudinary account (for image uploads) — optional

## Environments

Configuration is driven entirely by environment variables (see
[`.env.example`](.env.example) for the full, documented list). The
`ENVIRONMENT` variable (`local` | `dev` | `production`) controls CORS, whether
the interactive docs are exposed, and logging.

Ready-to-copy templates live in [`deploy/`](deploy/):

| File                     | Use                                             |
| ------------------------ | ----------------------------------------------- |
| `deploy/.env.local`      | Local development (pairs with docker-compose)   |
| `deploy/.env.dev`        | Dev / staging (fill via your host's secrets)    |
| `deploy/.env.production` | Production (fill via your host's secrets)       |

> Never commit a real `.env`. Only the placeholder templates are tracked.

### Required variables

`DATABASE_URL`, `SECRET_KEY` (generate with `make secret`), and
`OPENAI_API_KEY` are required for the app to be useful. `CORS_ORIGINS` must
list your frontend origin(s). See `.env.example` for everything else.

## Quick start

### Option A — Docker (recommended for local)

```bash
cp deploy/.env.local .env      # then set OPENAI_API_KEY, Cloudinary keys
make up                        # starts Postgres + API, runs migrations
# API  → http://localhost:8000
# Docs → http://localhost:8000/docs
```

### Option B — Bare metal

```bash
python -m venv venv && source venv/bin/activate
make install
make env                       # creates .env from the local template
# ...edit .env (point DATABASE_URL at your Postgres)...
make migrate                   # alembic upgrade head
make run                       # uvicorn with autoreload
```

Run `make help` to see all workflow commands.

## Database migrations

```bash
make revision m="add orders table"   # autogenerate from model changes
make migrate                         # apply
make downgrade                       # roll back the last one
```

Migrations run automatically on container start via `deploy/entrypoint.sh`.

## Dev / Production deployment

The [`Dockerfile`](Dockerfile) builds a production image that runs as a
non-root user under Gunicorn with Uvicorn workers, exposes `/health`, and
applies migrations on boot.

```bash
make build                     # build apsara-api:latest
```

Deploy that image to any container host (Railway, Render, Fly.io, ECS, etc.):

1. Set the environment variables from `deploy/.env.dev` /
   `deploy/.env.production` in the host's **secret manager** (never in git).
2. Point the host's health check at `GET /health`.
3. Provision a managed Postgres and set `DATABASE_URL` (use `?sslmode=require`).
4. Set `PUBLIC_BASE_URL` to the API's public URL so platform webhook
   callbacks resolve correctly.
5. Scale horizontally by running more replicas and/or raising Gunicorn workers
   (`-w`) — the app is stateless (JWT auth), so replicas need no shared state
   beyond Postgres.

## Health

`GET /health` → `{"status": "ok", "environment": "..."}` — use for load-balancer
and container health checks.
