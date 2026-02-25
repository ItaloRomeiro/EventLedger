import datetime as dt
import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import SQLModel, Session, delete, select

from app.models import Customer, Payment, Subscription, WebhookEvent
from app.repositories import create_db_and_tables, engine
from main import app


def _headers(body: str, secret: str = "test_secret", timestamp: int | None = None) -> dict[str, str]:
    ts = timestamp or int(dt.datetime.utcnow().timestamp())
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": signature,
        "Content-Type": "application/json",
    }


def _reset_db() -> None:
    SQLModel.metadata.drop_all(engine)
    create_db_and_tables()
    with Session(engine) as session:
        session.exec(delete(Payment))
        session.exec(delete(Subscription))
        session.exec(delete(Customer))
        session.exec(delete(WebhookEvent))
        session.commit()


@pytest.mark.anyio
async def test_invalid_signature_rejected() -> None:
    _reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {"event_id": "evt_invalid_sig", "event_type": "payment.succeeded", "payload_json": {}}
        body = json.dumps(payload, separators=(",", ":"))
        headers = _headers(body, secret="wrong_secret")
        response = await client.post("/v1/webhooks/test", content=body, headers=headers)
        assert response.status_code == 403


@pytest.mark.anyio
async def test_pending_to_active_and_idempotent_duplicate() -> None:
    _reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sub_response = await client.post(
            "/v1/subscriptions",
            json={"customer_email": "x@y.com", "plan_id": 1},
        )
        assert sub_response.status_code == 200
        sub_data = sub_response.json()

        future_period_end = (dt.datetime.utcnow() + dt.timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        payload = {
            "provider_customer_id": sub_data["provider_customer_id"],
            "provider_subscription_id": sub_data["provider_subscription_id"],
            "amount": 5000,
            "currency": "BRL",
            "current_period_end": future_period_end,
            "payment_id": "pay_ok",
            "invoice_id": "inv_ok",
        }
        event = {"event_id": "evt_ok_1", "event_type": "payment.succeeded", "payload_json": payload}
        body = json.dumps(event, separators=(",", ":"))
        headers = _headers(body)

        first = await client.post("/v1/webhooks/test", content=body, headers=headers)
        assert first.status_code == 200
        assert first.json()["processing_status"] == "processed"

        second = await client.post("/v1/webhooks/test", content=body, headers=headers)
        assert second.status_code == 200
        assert second.json()["event_id"] == "evt_ok_1"

    with Session(engine) as session:
        sub = session.exec(select(Subscription).where(Subscription.id == sub_data["subscription_id"])).first()
        assert sub is not None
        assert sub.status == "active"
        payment_count = len(session.exec(select(Payment).where(Payment.subscription_id == sub.id)).all())
        assert payment_count == 1


@pytest.mark.anyio
async def test_active_to_past_due_to_canceled_via_grace_job() -> None:
    _reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sub_response = await client.post(
            "/v1/subscriptions",
            json={"customer_email": "z@y.com", "plan_id": 1},
        )
        sub_data = sub_response.json()

        future_period_end = (dt.datetime.utcnow() + dt.timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        pay_payload = {
            "provider_customer_id": sub_data["provider_customer_id"],
            "provider_subscription_id": sub_data["provider_subscription_id"],
            "amount": 5000,
            "currency": "BRL",
            "current_period_end": future_period_end,
        }
        pay_event = {"event_id": "evt_to_active", "event_type": "payment.succeeded", "payload_json": pay_payload}
        pay_body = json.dumps(pay_event, separators=(",", ":"))
        assert (await client.post("/v1/webhooks/test", content=pay_body, headers=_headers(pay_body))).status_code == 200

        fail_payload = {
            "provider_customer_id": sub_data["provider_customer_id"],
            "provider_subscription_id": sub_data["provider_subscription_id"],
            "amount": 5000,
            "currency": "BRL",
            "current_period_end": future_period_end,
        }
        fail_event = {
            "event_id": "evt_to_past_due",
            "event_type": "invoice.payment_failed",
            "payload_json": fail_payload,
        }
        fail_body = json.dumps(fail_event, separators=(",", ":"))
        fail_response = await client.post("/v1/webhooks/test", content=fail_body, headers=_headers(fail_body))
        assert fail_response.status_code == 200

    with Session(engine) as session:
        sub = session.exec(select(Subscription).where(Subscription.id == sub_data["subscription_id"])).first()
        assert sub is not None
        assert sub.status == "past_due"
        sub.past_due_since = dt.datetime.utcnow() - dt.timedelta(days=2)
        session.add(sub)
        session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        grace = await client.post("/v1/jobs/enforce-grace")
        assert grace.status_code == 200

    with Session(engine) as session:
        sub = session.exec(select(Subscription).where(Subscription.id == sub_data["subscription_id"])).first()
        assert sub is not None
        assert sub.status == "canceled"
