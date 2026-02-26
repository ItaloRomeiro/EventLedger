# FastAPI Webhook Billing Case

Production-oriented SaaS subscription flow focused on **robust webhook handling**, **security**, and **state consistency**.

---

## 🚀 Overview

This project implements a subscription billing backend with:

- Transactional idempotency at the database level
- HMAC signature validation with anti-replay protection
- Internal retries and basic DLQ mechanism
- API versioning (`/v1`)
- Provider simulator for QA and demos

---

# 🏗 Architecture

## Layers

- **app/api.py**  
  FastAPI endpoints, signature validation, rate limiting, allowlist.

- **app/services.py**  
  Use cases (process_webhook, create_subscription, jobs, retries).

- **app/models.py**  
  SQLModel entities + Pydantic schemas + enums.

- **app/repositories.py**  
  Engine/session setup and table bootstrap.

---

# 📥 Webhook Flow (Inbox Pattern)

1. Receives `POST /v1/webhooks/{provider}`
2. Validates signature and timestamp at the edge
3. Attempts to insert `WebhookEvent` with `UNIQUE(provider, event_id)`
4. Processes domain event (handler per `event_type`)
5. Marks as `processed / ignored / failed` inside the same transaction

---

# 🔁 Idempotency & Atomicity

### Database Constraint

```sql
UNIQUE(provider, event_id)
```

### Under Parallel Concurrency

- First request → inserts and processes
- Second request → raises `IntegrityError`
- Event is reloaded and idempotency logic is applied

### Anti-Replay Logic

If:
- `signature_timestamp` differs → `403 + failed`
- `signature` differs → `403 + failed`

---

# 🧾 Subscription State Machine

## States

- `pending_activation`
- `active`
- `past_due`
- `canceled`
- `expired`

## State Diagram

```
pending_activation --payment.succeeded--> active
active --invoice.payment_failed--> past_due
past_due --payment.succeeded--> active
past_due --enforce_grace(job)--> canceled
active --expire_subscriptions(job)--> expired
active + cancel_at_period_end=true --expire_subscriptions(job)--> canceled
```

---

## Governance Fields

- `cancel_at_period_end`
- `past_due_since`
- `canceled_at`
- `expired_at`
- `access_revoked`

---

# 🔐 Security

### Implemented

- HMAC SHA-256  
  (`X-Webhook-Timestamp + X-Webhook-Signature`)
- Replay window validation (`WEBHOOK_MAX_SKEW_SECONDS`)
- In-memory rate limiting per provider + IP
- Optional IP allowlist (`WEBHOOK_IP_ALLOWLIST`)
- Environment-based secrets with rotation (`WEBHOOK_SECRETS_JSON`)
- Key selection via `X-Webhook-Key-Id`

---

## Example `WEBHOOK_SECRETS_JSON`

```json
{
  "stripe": {
    "current": "stripe_secret_v2",
    "previous": ["stripe_secret_v1"],
    "keys": {
      "kid-2026-02": "stripe_secret_v2"
    }
  },
  "mercadopago": "mp_secret_here",
  "test": "test_secret"
}
```

---

# 🔄 Retry & DLQ

## WebhookEvent Fields

- `attempt_count`
- `next_retry_at`
- `needs_attention` (basic DLQ after 3 failures)

## Retry Endpoints

- `POST /v1/jobs/retry-failed-webhooks`
- `POST /v1/admin/webhooks/{event_id}/reprocess`

---

# 🧩 Versioning & Contract

API is versioned under:

```
/v1
```

### Event Schemas

- `PaymentSucceededPayload`
- `InvoicePaymentFailedPayload`

---

# 📦 Incoming Webhook Example

```json
{
  "event_id": "evt_123",
  "event_type": "payment.succeeded",
  "payload_json": {
    "provider_customer_id": "cus_123",
    "provider_subscription_id": "sub_456",
    "amount": 5000,
    "currency": "BRL",
    "current_period_end": "2026-02-24T12:00:00Z",
    "payment_id": "pay_789",
    "invoice_id": "inv_001"
  }
}
```

---

# 🧪 Provider Simulator

Endpoint:

```
POST /v1/simulate/{provider}/{event_type}
```

The simulator:

- Generates a normalized payload
- Signs it using provider secret
- Internally calls `/v1/webhooks/{provider}`
- Returns webhook status and response

### Example

```bash
curl -X POST http://127.0.0.1:8000/v1/simulate/test/payment.succeeded
```

---

# 🖥 Running Locally

## Install dependencies

```bash
poetry install --no-root
```

## Start API

```bash
poetry run uvicorn main:app --reload
```

---

# 📊 Metrics

- Internal JSON:  
  `GET /v1/admin/metrics`

- Prometheus format:  
  `GET /v1/metrics`

---

# 🧪 Testing

```bash
poetry run pytest -q
```

---

# ⚖️ Decisions & Tradeoffs

- **SQLite + SQLModel**  
  Chosen for fast local iteration. Migrating to PostgreSQL is straightforward.

- **Retry/DLQ in database**  
  Simplifies initial operation. External queue recommended at scale.

- **In-memory rate limiting & allowlist**  
  Suitable for demo/dev. Production should use gateway or Redis.

- **create_all usage**  
  Kept for simplicity. In continuous production environments, Alembic migrations are recommended.

---

# 🎯 Production Notes

This implementation demonstrates:

- Strong idempotency guarantees
- Transactional integrity
- Security-first webhook validation
- Clear subscription state management
- Operational observability

Designed for real-world SaaS billing scenarios.
