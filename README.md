# FastAPI Webhook Billing Case

Este projeto implementa um fluxo SaaS de assinaturas com foco em webhook de produção:
- idempotência transacional no banco;
- segurança de assinatura (HMAC) com anti-replay;
- retries internos e DLQ básico;
- versionamento de API (`/v1`);
- simulador de provedor para QA/demo.

## Arquitetura
Camadas:
- `app/api.py`: endpoints FastAPI, validação de assinatura, rate-limit, allowlist.
- `app/services.py`: casos de uso (`process_webhook`, `create_subscription`, jobs, retries).
- `app/models.py`: entidades SQLModel + schemas Pydantic + enums.
- `app/repositories.py`: engine/session e bootstrap de tabelas.

Fluxo de webhook (inbox-style):
1. Recebe `POST /v1/webhooks/{provider}`.
2. Valida assinatura e timestamp na borda.
3. Tenta inserir `WebhookEvent` com `UNIQUE(provider, event_id)`.
4. Processa evento de domínio (handlers por `event_type`).
5. Marca `processed`/`ignored`/`failed` na mesma unidade transacional.

## Idempotência e Atomicidade
- Constraint: `UNIQUE(provider, event_id)` em `WebhookEvent`.
- Em concorrência paralela:
  - primeira request insere e processa;
  - segunda request recebe `IntegrityError`, recarrega o evento e aplica fluxo de idempotência.
- Anti-replay para duplicado:
  - se `signature_timestamp` diverge: `403` + `failed`;
  - se `signature` diverge: `403` + `failed`.

## Estado da Assinatura
Estados:
- `pending_activation`
- `active`
- `past_due`
- `canceled`
- `expired`

Diagrama simples:
```text
pending_activation --payment.succeeded--> active
active --invoice.payment_failed--> past_due
past_due --payment.succeeded--> active
past_due --enforce_grace(job)--> canceled
active --expire_subscriptions(job)--> expired
active + cancel_at_period_end=true --expire_subscriptions(job)--> canceled
```

Campos de governança:
- `cancel_at_period_end`
- `past_due_since`
- `canceled_at`
- `expired_at`
- `access_revoked`

## Segurança
Implementado:
- HMAC SHA-256 (`X-Webhook-Timestamp` + `X-Webhook-Signature`).
- Replay window por timestamp (`WEBHOOK_MAX_SKEW_SECONDS`).
- Rate limit in-memory por `provider+ip`.
- IP allowlist opcional (`WEBHOOK_IP_ALLOWLIST`).
- Segredos por ambiente (`WEBHOOK_SECRETS_JSON`) com rotação:
  - `current`
  - `previous`
  - `keys` por `X-Webhook-Key-Id`

Exemplo `WEBHOOK_SECRETS_JSON`:
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

## Retry e DLQ
`WebhookEvent` possui:
- `attempt_count`
- `next_retry_at`
- `needs_attention` (DLQ básico após 3 falhas)

Jobs/endpoints:
- `POST /v1/jobs/retry-failed-webhooks`
- `POST /v1/admin/webhooks/{event_id}/reprocess`

## Versionamento e Contrato
API versionada em `/v1`.

Schemas por tipo de evento:
- `PaymentSucceededPayload`
- `InvoicePaymentFailedPayload`

Webhook de entrada:
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

## Simulador de Provedor
Endpoint:
- `POST /v1/simulate/{provider}/{event_type}`

O simulador:
1. Gera payload normalizado válido.
2. Assina com o secret do provedor.
3. Chama internamente `/v1/webhooks/{provider}`.
4. Retorna status e resposta do webhook.

Exemplo:
```bash
curl -X POST http://127.0.0.1:8000/v1/simulate/test/payment.succeeded
```

## Como Rodar Local
1. Instalar dependências:
```bash
poetry install --no-root
```

2. Subir API:
```bash
poetry run uvicorn main:app --reload
```

3. Métricas:
- JSON interno: `GET /v1/admin/metrics`
- Prometheus: `GET /v1/metrics`

## Como Testar
```bash
poetry run pytest -q
```

## Decisões e Tradeoffs
- SQLite + SQLModel para acelerar iteração local; migração para Postgres é natural.
- Retry/DLQ no próprio banco simplifica operação inicial, mas fila externa é recomendada em escala.
- Rate-limit/allowlist in-memory são suficientes para demo e dev; em produção distribuída usar gateway/redis.
- `create_all` foi mantido por simplicidade; para produção contínua, usar Alembic.

