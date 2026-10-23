<div align="center">

# Apsara Assistant — Backend

**An AI sales assistant for Cambodian online sellers that answers customers, takes orders, and checks payments in Khmer and English.**

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-D71F00?style=for-the-badge&logo=sqlalchemy&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)

</div>

---

## Overview

Apsara Assistant is a multi-tenant sales assistant for Cambodian shops. Customers message a seller's Facebook Page or Telegram bot. The assistant answers from the seller's own catalogue and shop information in Khmer, English, or romanized Khmer, and it can draft an order and take a payment while the seller is away.

This repository is the FastAPI backend. The seller dashboard lives in [Apsara-Assistant-Web](https://github.com/RithyBondeth/Apsara-Assistant-Web).

## Features

- **AI replies grounded in the shop.** The catalogue, prices, address, hours, delivery and policies are rendered into the prompt. When the model is unsure, it escalates to the seller instead of guessing.
- **Large catalogues.** Up to 100 products are shown to the assistant in full. Beyond that, it sees the products the conversation is about (matched by name, including Khmer written without spaces, by description words, or by SKU) and then best sellers, with the rest listed by name only.
- **Product photos in chat.** The assistant can send a product's primary photo when a customer asks to see it, using an id-checked marker in a single model round trip.
- **Unified inbox** for Facebook Messenger and Telegram, with idempotent message handling and a manual-mode takeover.
- **Orders and payments.** AI order drafts, Stripe Checkout with each seller's own account, and payment-QR support for sellers who cannot use Stripe.
- **Order updates to the customer.** Confirmed, shipped, delivered, cancelled and paid each send the customer one message in their thread, in Khmer or English. Each is sent once, can be skipped per change (`notify_customer: false`), and can be turned off per shop (`customer_updates_enabled`).
- **Dynamic KHQR with Bakong confirmation.** Each order can get a KHQR for its exact amount and currency. The assistant sends it in place of the static QR when an order is awaiting payment, and the order is marked paid as soon as Bakong reports the transaction (checked by the MD5 of the code).
- **Receipt verification.** A vision model reads customer receipt photos and returns a verdict against the order: `match`, `amount_mismatch`, `duplicate`, and others.
- **Dual currency (USD/KHR)** at each shop's own exchange rate.
- **Seller alerts** sent to the seller's phone through Telegram, once per episode, in the seller's language.
- **Products, variants, images, inventory, customers, and an operations suite** (purchasing and returns).
- **Media in Postgres or any S3-compatible bucket** (`MEDIA_STORAGE`). Files stay behind the API, deletions are tracked by a database trigger, and `python -m app.scripts.move_media` moves existing files.
- **Durable job queue** on PostgreSQL (`SKIP LOCKED`), run inline or as a separate worker.
- **Security:** encrypted third-party credentials at rest, HttpOnly cookie sessions, OTP and password reset, sign-in throttling, and signed webhooks.
- **Cost controls:** per-seller daily ceilings for AI replies, drafts, and receipt scans.
- **Observability:** structured logs, request IDs, optional Sentry, and liveness and readiness probes.

## Tech Stack

| Area | Technology |
| --- | --- |
| Language | Python 3.12 |
| Framework | FastAPI, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy, Alembic |
| AI | OpenAI (chat and vision models) |
| Integrations | Facebook Messenger, Telegram Bot API, Stripe Checkout, SMTP |
| Infrastructure | Docker, Docker Compose, GitHub Actions CI |
| Testing | Pytest against a real PostgreSQL database |

## Getting Started

### Prerequisites

- Python 3.12
- PostgreSQL 16. SQLite is not supported, because the models use the UUID and JSONB types, `SKIP LOCKED`, and `ON CONFLICT`.
- Optional: Docker

### Installation

```bash
git clone https://github.com/RithyBondeth/Apsara-Assistant-Backend.git
cd Apsara-Assistant-Backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # set DATABASE_URL and SECRET_KEY
createdb apsara_db
alembic upgrade head
uvicorn app.main:app --reload
```

Interactive API docs are served at [http://localhost:8000/docs](http://localhost:8000/docs).

**With Docker:** `./dev-up.sh` starts PostgreSQL, applies migrations, and runs the API with its worker. Add `-d` to run it in the background.

### Environment Variables

Every setting is documented in [`.env.example`](.env.example). The key ones:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | Signs tokens and derives the credential-encryption key |
| `CORS_ORIGINS` | Allowed web origins (required outside development) |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_VISION_MODEL` | AI replies and receipt reading |
| `META_APP_SECRET`, `META_VERIFY_TOKEN` | Facebook Messenger webhooks |
| `JOB_RUNNER` | `inline`, or `worker` (run `python -m app.worker`) |
| `BAKONG_API_URL`, `BAKONG_API_TOKEN` | Bakong Open API for KHQR payment checks (Cambodian IPs only; use a relay when hosted abroad) |
| `MEDIA_STORAGE`, `S3_*` | `database`, or `s3` with a bucket for photos, QRs and attachments |
| `AI_DAILY_REPLY_LIMIT`, `AI_DAILY_DRAFT_LIMIT`, `AI_DAILY_RECEIPT_LIMIT` | Per-seller daily AI spending ceilings |

### Running Tests

```bash
createdb apsara_test_db
pytest
```

## Project Structure

```text
Apsara-Assistant-Backend/
├── app/
│   ├── api/v1/endpoints/   # auth, products, customers, orders, conversations, chat,
│   │                       # inventory, operations, integrations, payment QRs, webhooks
│   ├── services/           # AI, inbound messages, platforms, Stripe, queue, quotas,
│   │                       # receipts, seller alerts, verification
│   ├── models/             # SQLAlchemy models
│   ├── schemas/            # Pydantic schemas
│   ├── core/               # config, security, crypto, clock, logging, observability
│   ├── main.py
│   └── worker.py           # Background job worker
├── alembic/                # Database migrations
├── docs/                   # Channel connection guide
├── tests/
├── Dockerfile
└── docker-compose.yml
```

## Documentation

- [Connecting Messenger and Telegram channels](docs/connecting-channels.md)
- Health checks: `GET /health` (liveness) and `GET /health/ready` (readiness)

## Related Repositories

- [Apsara-Assistant-Web](https://github.com/RithyBondeth/Apsara-Assistant-Web): the seller dashboard and landing page

## Author

**Rithy Bondeth** — Full Stack Developer & AI Engineer

[![Portfolio](https://img.shields.io/badge/Portfolio-0A0A0A?style=for-the-badge&logo=vercel&logoColor=white)](https://bondeth.dev)
[![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/RithyBondeth)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/in/rithy-bondeth-484073282/)

## License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for details.
