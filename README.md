# Apsara Assistant — API

FastAPI backend for an AI sales assistant aimed at Cambodian shops. Customers
message a seller's Facebook Page or Telegram bot; the assistant answers from the
seller's own product catalogue, in Khmer, English or romanized Khmer, and can
take an order and a card payment without the seller being awake.

The web client lives in [`../Apsara-Assistant-Web`](../Apsara-Assistant-Web).

## Requirements

- **Python 3.12** — the code uses `X | None` unions and `datetime` behaviour
  that 3.9 does not have.
- **PostgreSQL 16** — not swappable for SQLite. The models use the `postgresql`
  UUID and JSONB dialects, `SELECT … FOR UPDATE SKIP LOCKED` for the job queue,
  and `ON CONFLICT` for the reply quota.

## Getting started

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env` — `DATABASE_URL` and `SECRET_KEY` are the only two with no usable
default. Then create the schema and run it:

```bash
createdb apsara_db
alembic upgrade head
uvicorn app.main:app --reload
```

Interactive API docs are at `http://localhost:8000/docs`.

Everything else is optional and degrades quietly: with no `OPENAI_API_KEY` the
assistant endpoints return a clear error rather than failing at import; with no
`SMTP_HOST`, password-reset and OTP mail is written to the log instead of sent,
so local development needs no mail account.

### With Docker instead

Start PostgreSQL, apply migrations, and run the API plus its background worker
with one command:

```bash
./dev-up.sh
```

The script reads `.env`, checks that Docker and `SECRET_KEY` are available, and
preserves database data in the `apsara-db` Docker volume. Pass `-d` to run in
the background:

```bash
./dev-up.sh -d
```

Stop a background stack with `docker compose down`. This removes the
containers but keeps the database volume. To provide configuration from the
shell instead of `.env`, environment variables still work as normal, for
example `SECRET_KEY=dev-secret ./dev-up.sh`.

## Tests

```bash
pytest
```

The suite runs against a **real Postgres**, not an in-memory stand-in, for the
dialect reasons above. It uses `TEST_DATABASE_URL` (default
`postgresql://localhost:5432/apsara_test_db`) and wipes that database
repeatedly — point it at a throwaway one.

```bash
createdb apsara_test_db
```

## How it fits together

```
app/
  api/v1/endpoints/   HTTP surface — auth, products, customers, orders,
                      conversations, chat, integrations, webhooks
  services/           the parts with actual behaviour:
                        ai_service      prompt building and the OpenAI call
                        inbound         turning a webhook into a reply
                        platforms       Messenger and Telegram
                        stripe_gateway  card payments
                        queue           durable jobs on Postgres
                        quota           daily reply ceiling
                        throttle        sign-in rate limiting
                        verification    password-reset and OTP codes
  models/             SQLAlchemy tables
  core/               config, security, crypto, clock, logging
alembic/versions/     migrations
```

A few decisions worth knowing before changing things:

**Every query is scoped to the signed-in seller.** This is a multi-tenant
database with no row-level security behind it, so `user_id == current_user.id`
in the query *is* the isolation. A new endpoint that forgets it leaks another
shop's data.

**Inbound work is queued, not done in the request.** A webhook that does not get
a prompt 2xx is retried, sometimes for hours, and the work behind one includes an
OpenAI round trip. `enqueue` writes a row, and the row is what survives a
restart. `JOB_RUNNER=inline` drains it in the web process (fine for one
process); `JOB_RUNNER=worker` leaves it to `python -m app.worker`.

**Third-party credentials are encrypted at rest** (`app/core/crypto.py`). A
stored page token, bot token or Stripe key is a live credential for someone
else's account. The key derives from `SECRET_KEY` unless `PLATFORM_TOKEN_KEY` is
set — meaning rotating `SECRET_KEY` alone makes stored tokens unreadable and
sellers must reconnect.

**Timestamps go through `app/core/clock.py`.** The columns are naive
`TIMESTAMP WITHOUT TIME ZONE` holding UTC; `utcnow()` matches that. Don't reach
for `datetime.now(timezone.utc)` — comparing an aware value against these
columns raises.

**Webhooks prove who is calling.** They are the only unauthenticated endpoints:
Messenger by an app-level signature over the raw body, Telegram by a
per-connection secret header, Stripe by its signature over the raw body. They
answer 200 to anything they cannot use, because a 4xx buys a retry storm rather
than a fix.

## Integrations

Connecting a Page or bot is documented step by step in
[`docs/connecting-channels.md`](docs/connecting-channels.md), including the
tunnel needed to receive webhooks locally.

### Payments

Card payments go through **Stripe Checkout**, with each seller connecting their
own Stripe account under Integrations. Calls are made with that seller's key, so
money moves from the customer to the seller and never through this platform.
The seller supplies two things: a secret key (a restricted key with write access
to Checkout Sessions is enough) and the signing secret of a webhook endpoint
they add in Stripe pointing at `/api/v1/webhooks/stripe/{connection_id}`,
subscribed to `checkout.session.completed`.

An order is only marked paid by that webhook. The customer landing back on a
success URL proves nothing — it can be visited directly and does not happen at
all if they close the tab.

> **Stripe is not available to businesses based in Cambodia.** Sellers who
> cannot onboard there use the payment-QR flow instead (`payment_qr_url` on the
> seller). Both are supported; neither is required.

## Configuration

Every setting, with comments, is in [`.env.example`](.env.example). The ones
that bite:

| Setting | Why it matters |
| --- | --- |
| `SECRET_KEY` | Signs tokens, keys the OTP hash, and derives the credential-encryption key. Rotating it invalidates stored platform tokens. |
| `CORS_ORIGINS` | Required outside development — the app refuses to start rather than allow credentialed requests from any origin. |
| `TRUST_PROXY_HEADERS` | Leave false unless a proxy you control sets `X-Forwarded-For`. If nothing strips an inbound header, a caller can forge it and step around the per-address sign-in limit. |
| `JOB_RUNNER` | `inline` or `worker`. With `worker`, something must actually run `python -m app.worker`, or queued replies are never sent. |
| `AI_DAILY_REPLY_LIMIT` | Inbound volume is not ours to control; this is the ceiling on OpenAI spend per seller per day. |
| `AI_DAILY_DRAFT_LIMIT` | Separate daily ceiling for seller-triggered AI order proposals. |

## Operational endpoints

- `GET /health` — liveness. Deliberately does not touch the database, so a
  database blip cannot take every instance out at once.
- `GET /health/ready` — readiness. Returns 503 when Postgres is unreachable.

Every response carries an `X-Request-ID`, honouring an inbound one so a trace
started at a proxy or in the web app carries through.
