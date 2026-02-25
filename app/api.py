import datetime as dt
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from app.models import (
    InvalidPayloadError,
    NotFoundError,
    ReplayAttackError,
    Subscription,
    SubscriptionCancelAtPeriodEndIn,
    SubscriptionCreateIn,
    SubscriptionCreateOut,
    VerifiedWebhookData,
    WebhookEventIn,
)
from app.repositories import engine
from app.services import (
    create_subscription,
    expire_subscriptions,
    enforce_grace_period,
    get_metrics,
    get_webhook_event,
    list_webhook_events,
    process_webhook,
    reprocess_webhook_event,
    retry_failed_webhooks,
    set_subscription_cancel_at_period_end,
)


router = APIRouter(prefix="/v1")

DEFAULT_WEBHOOK_SECRETS: dict[str, str] = {
    "stripe": "stripe_secret_here",
    "mercadopago": "mp_secret_here",
    "test": "test_secret",
}
WEBHOOK_MAX_SKEW_SECONDS = 300
WEBHOOK_RATE_LIMIT_PER_MINUTE = int(os.getenv("WEBHOOK_RATE_LIMIT_PER_MINUTE", "120"))
WEBHOOK_IP_ALLOWLIST = {ip.strip() for ip in os.getenv("WEBHOOK_IP_ALLOWLIST", "").split(",") if ip.strip()}
_webhook_request_windows: dict[str, deque[int]] = defaultdict(deque)


def _load_webhook_secrets() -> dict[str, str]:
    raw = os.getenv("WEBHOOK_SECRETS_JSON")
    if not raw:
        return DEFAULT_WEBHOOK_SECRETS
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid WEBHOOK_SECRETS_JSON") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("WEBHOOK_SECRETS_JSON must be a JSON object")
    return {str(k): str(v) for k, v in loaded.items()}


WEBHOOK_SECRETS = _load_webhook_secrets()


def _get_secret_candidates(provider: str, key_id: str | None) -> list[str]:
    provider_cfg = WEBHOOK_SECRETS.get(provider)
    if provider_cfg is None:
        return []
    if isinstance(provider_cfg, str):
        return [provider_cfg]
    if not isinstance(provider_cfg, dict):
        return []

    candidates: list[str] = []
    if key_id and isinstance(provider_cfg.get("keys"), dict):
        key_map = provider_cfg["keys"]
        maybe = key_map.get(key_id)
        if isinstance(maybe, str):
            candidates.append(maybe)
    current_secret = provider_cfg.get("current")
    if isinstance(current_secret, str):
        candidates.append(current_secret)
    previous_secrets = provider_cfg.get("previous", [])
    if isinstance(previous_secrets, list):
        candidates.extend([s for s in previous_secrets if isinstance(s, str)])

    # Keep order and remove duplicates.
    seen: set[str] = set()
    deduped: list[str] = []
    for secret in candidates:
        if secret in seen:
            continue
        seen.add(secret)
        deduped.append(secret)
    return deduped


def _get_signing_secret(provider: str) -> str:
    candidates = _get_secret_candidates(provider, key_id=None)
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"provider '{provider}' has no configured secret",
        )
    return candidates[0]


def _build_simulation_payload(provider: str, event_type: str) -> tuple[dict, dict]:
    email = f"simulate-{uuid.uuid4().hex[:8]}@example.com"
    with Session(engine) as session:
        subscription = create_subscription(
            session,
            SubscriptionCreateIn(customer_email=email, plan_id=1),
        )

    base_payload = {
        "provider_customer_id": subscription.provider_customer_id,
        "provider_subscription_id": subscription.provider_subscription_id,
        "amount": 5000,
        "currency": "BRL" if provider == "mercadopago" else "USD",
        "current_period_end": (dt.datetime.utcnow() + dt.timedelta(days=30)).replace(microsecond=0).isoformat() + "Z",
        "payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "invoice_id": f"inv_{uuid.uuid4().hex[:10]}",
    }
    event = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "payload_json": base_payload,
    }
    return event, base_payload


async def validate_webhook_signature(provider: str, request: Request) -> VerifiedWebhookData:
    key_id = request.headers.get("X-Webhook-Key-Id")
    candidate_secrets = _get_secret_candidates(provider, key_id)
    if not candidate_secrets:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown webhook provider",
        )

    timestamp = request.headers.get("X-Webhook-Timestamp")
    signature = request.headers.get("X-Webhook-Signature")
    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing webhook signature headers",
        )

    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid webhook timestamp",
        ) from exc

    now_timestamp = int(dt.datetime.utcnow().timestamp())
    if abs(now_timestamp - timestamp_value) > WEBHOOK_MAX_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="webhook timestamp outside allowed window",
        )
    client_ip = request.client.host if request.client else "unknown"
    if WEBHOOK_IP_ALLOWLIST and client_ip not in WEBHOOK_IP_ALLOWLIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ip not allowed",
        )
    rate_key = f"{provider}:{client_ip}"
    window = _webhook_request_windows[rate_key]
    cutoff = now_timestamp - 60
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) >= WEBHOOK_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
        )
    window.append(now_timestamp)

    raw_body_bytes = await request.body()
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body_bytes
    signature_ok = False
    for secret in candidate_secrets:
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(signature, expected_signature):
            signature_ok = True
            break
    if not signature_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid webhook signature",
        )

    try:
        raw_body = raw_body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid webhook body encoding",
        ) from exc
    return VerifiedWebhookData(
        raw_body=raw_body,
        signature=signature,
        timestamp=timestamp_value,
    )


@router.post("/subscriptions", response_model=SubscriptionCreateOut)
async def create_subscription_endpoint(subscription_in: SubscriptionCreateIn):
    try:
        with Session(engine) as session:
            return create_subscription(session, subscription_in)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/webhooks/{provider}")
async def webhook_receiver(
    provider: str,
    verified: VerifiedWebhookData = Depends(validate_webhook_signature),
):
    try:
        webhook = WebhookEventIn.model_validate_json(verified.raw_body)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook body") from exc

    try:
        with Session(engine) as session:
            return process_webhook(session=session, provider=provider, webhook=webhook, verified=verified)
    except ReplayAttackError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive branch
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal processing error",
        ) from exc


@router.get("/webhooks")
async def list_webhook_events_endpoint():
    with Session(engine) as session:
        return list_webhook_events(session)


@router.get("/webhooks/{event_id}")
async def get_webhook_event_endpoint(
    event_id: str,
    provider: str | None = None,
):
    try:
        with Session(engine) as session:
            return get_webhook_event(session, event_id, provider=provider)
    except InvalidPayloadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/jobs/enforce-grace")
async def enforce_grace_period_endpoint():
    with Session(engine) as session:
        return enforce_grace_period(session)


@router.post("/jobs/expire-subscriptions")
async def expire_subscriptions_endpoint():
    with Session(engine) as session:
        return expire_subscriptions(session)


@router.post("/jobs/retry-failed-webhooks")
async def retry_failed_webhooks_endpoint(limit: int = 50):
    with Session(engine) as session:
        return retry_failed_webhooks(session=session, limit=limit)


@router.post("/admin/webhooks/{event_id}/reprocess")
async def reprocess_webhook_event_endpoint(event_id: str):
    try:
        with Session(engine) as session:
            return reprocess_webhook_event(session, event_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/subscriptions/{subscription_id}/cancel-at-period-end", response_model=Subscription)
async def set_cancel_at_period_end_endpoint(
    subscription_id: int,
    payload: SubscriptionCancelAtPeriodEndIn,
):
    try:
        with Session(engine) as session:
            return set_subscription_cancel_at_period_end(session, subscription_id, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/admin/metrics")
async def metrics_endpoint():
    return get_metrics()


@router.get("/metrics")
async def prometheus_metrics_endpoint() -> Response:
    metrics = get_metrics()
    body_lines = [
        "# HELP webhook_processed_total Number of processed webhook events.",
        "# TYPE webhook_processed_total counter",
        f"webhook_processed_total {metrics.get('webhook_processed', 0)}",
        "# HELP webhook_failed_total Number of failed webhook events.",
        "# TYPE webhook_failed_total counter",
        f"webhook_failed_total {metrics.get('webhook_failed', 0)}",
        "# HELP webhook_ignored_total Number of ignored webhook events.",
        "# TYPE webhook_ignored_total counter",
        f"webhook_ignored_total {metrics.get('webhook_ignored', 0)}",
        "# HELP webhook_replayed_total Number of replayed idempotent webhook events.",
        "# TYPE webhook_replayed_total counter",
        f"webhook_replayed_total {metrics.get('webhook_replayed', 0)}",
    ]
    return Response(
        content="\n".join(body_lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.post("/simulate/{provider}/{event_type}")
async def simulate_provider_event(provider: str, event_type: str, request: Request):
    if event_type not in {"payment.succeeded", "invoice.payment_failed"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unsupported event_type for simulator",
        )

    event, normalized_payload = _build_simulation_payload(provider, event_type)
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    timestamp = int(time.time())
    signing_secret = _get_signing_secret(provider)
    signature = hmac.new(
        signing_secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Signature": signature,
        "Content-Type": "application/json",
    }

    async with AsyncClient(transport=ASGITransport(app=request.app), base_url="http://internal") as client:
        response = await client.post(f"/v1/webhooks/{provider}", content=body, headers=headers)
    return {
        "simulated_provider": provider,
        "simulated_event_type": event_type,
        "normalized_payload": normalized_payload,
        "webhook_status_code": response.status_code,
        "webhook_response": response.json(),
    }
