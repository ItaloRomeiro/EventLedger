import datetime as dt
import json
import logging
import uuid
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    Customer,
    InvoicePaymentFailedPayload,
    InvalidPayloadError,
    NotFoundError,
    Payment,
    PaymentSucceededPayload,
    PaymentStatus,
    ReplayAttackError,
    Subscription,
    SubscriptionCancelAtPeriodEndIn,
    SubscriptionCreateIn,
    SubscriptionCreateOut,
    SubscriptionStatus,
    VerifiedWebhookData,
    WebhookEvent,
    WebhookEventIn,
    WebhookProcessingStatus,
)


logger = logging.getLogger(__name__)
METRICS: dict[str, int] = {
    "webhook_processed": 0,
    "webhook_failed": 0,
    "webhook_ignored": 0,
    "webhook_replayed": 0,
}


def _parse_period_end(value: Any) -> dt.datetime:
    if isinstance(value, (int, float)):
        return dt.datetime.utcfromtimestamp(value)
    if isinstance(value, str):
        iso_value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_value)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return dt.datetime.utcnow()


def _get_customer_by_provider_id(session: Session, provider_customer_id: str) -> Customer:
    statement = select(Customer).where(Customer.provider_customer_id == provider_customer_id)
    customer = session.exec(statement).first()
    if customer is None:
        raise InvalidPayloadError("provider_customer_id not found")
    return customer


def _get_or_create_customer_by_email(session: Session, customer_email: str) -> Customer:
    statement = select(Customer).where(Customer.email == customer_email)
    customer = session.exec(statement).first()
    if customer is not None:
        return customer

    customer = Customer(
        email=customer_email,
        status="active",
    )
    session.add(customer)
    session.flush()
    return customer


def _ensure_provider_customer_id(session: Session, customer: Customer) -> str:
    if customer.provider_customer_id:
        return customer.provider_customer_id

    provider_customer_id = f"cus_{uuid.uuid4().hex[:16]}"
    customer.provider_customer_id = provider_customer_id
    session.add(customer)
    session.flush()
    return provider_customer_id


def _get_subscription_by_provider_id(
    session: Session,
    provider_subscription_id: str,
    customer_id: int,
) -> Subscription:
    statement = select(Subscription).where(Subscription.provider_subscription_id == provider_subscription_id)
    subscription = session.exec(statement).first()
    if subscription is None:
        raise InvalidPayloadError("provider_subscription_id not found")
    if subscription.customer_id != customer_id:
        raise InvalidPayloadError("provider_subscription_id belongs to a different customer_id")
    return subscription


def _handle_payment_succeeded(session: Session, event: WebhookEvent, payload_data: dict[str, Any]) -> None:
    payload = PaymentSucceededPayload.model_validate(payload_data)
    provider_subscription_id = payload.provider_subscription_id
    provider_customer_id = payload.provider_customer_id
    customer = _get_customer_by_provider_id(session, provider_customer_id)
    period_end = _parse_period_end(payload.current_period_end)

    subscription = _get_subscription_by_provider_id(
        session=session,
        provider_subscription_id=provider_subscription_id,
        customer_id=customer.id,
    )
    # Ignore stale events to avoid out-of-order regressions.
    if period_end < subscription.current_period_end:
        event.processing_status = WebhookProcessingStatus.ignored
        event.processed_at = dt.datetime.utcnow()
        event.error_message = "stale event ignored"
        return
    if subscription.status in (SubscriptionStatus.pending_activation, SubscriptionStatus.past_due):
        subscription.status = SubscriptionStatus.active
    subscription.canceled_at = None
    subscription.expired_at = None
    subscription.current_period_end = period_end
    subscription.past_due_since = None
    subscription.access_revoked = False
    subscription.updated_at = dt.datetime.utcnow()
    session.add(subscription)

    payment = Payment(
        customer_id=customer.id,
        subscription_id=subscription.id,
        status=PaymentStatus.approved,
        amount=payload.amount,
        currency=payload.currency,
        provider_payment_id=str(payload.payment_id or event.event_id),
        provider_invoice_id=str(payload.invoice_id or ""),
        processed_at=dt.datetime.utcnow(),
        provider=event.provider,
    )
    session.add(payment)


def _handle_invoice_payment_failed(session: Session, event: WebhookEvent, payload_data: dict[str, Any]) -> None:
    payload = InvoicePaymentFailedPayload.model_validate(payload_data)
    provider_subscription_id = payload.provider_subscription_id
    provider_customer_id = payload.provider_customer_id
    customer = _get_customer_by_provider_id(session, provider_customer_id)
    period_end = _parse_period_end(payload.current_period_end)

    subscription = _get_subscription_by_provider_id(
        session=session,
        provider_subscription_id=provider_subscription_id,
        customer_id=customer.id,
    )
    if period_end < subscription.current_period_end:
        event.processing_status = WebhookProcessingStatus.ignored
        event.processed_at = dt.datetime.utcnow()
        event.error_message = "stale event ignored"
        return
    if subscription.status == SubscriptionStatus.active:
        subscription.status = SubscriptionStatus.past_due
        subscription.past_due_since = dt.datetime.utcnow()
    subscription.updated_at = dt.datetime.utcnow()
    session.add(subscription)

    payment = Payment(
        customer_id=customer.id,
        subscription_id=subscription.id,
        status=PaymentStatus.refused,
        amount=payload.amount,
        currency=payload.currency,
        provider_payment_id=str(payload.payment_id or event.event_id),
        provider_invoice_id=str(payload.invoice_id or ""),
        processed_at=dt.datetime.utcnow(),
        provider=event.provider,
    )
    session.add(payment)


def _handle_unknown_event(_: Session, event: WebhookEvent, __: dict[str, Any]) -> None:
    event.processing_status = WebhookProcessingStatus.ignored
    event.processed_at = dt.datetime.utcnow()


EVENT_HANDLERS: dict[str, Callable[[Session, WebhookEvent, dict[str, Any]], None]] = {
    "payment.succeeded": _handle_payment_succeeded,
    "invoice.payment_failed": _handle_invoice_payment_failed,
}


def dispatch_event(session: Session, event: WebhookEvent) -> None:
    event_payload = json.loads(event.payload_raw)
    if not isinstance(event_payload, dict):
        raise InvalidPayloadError("payload_json must be an object")
    payload_data = event_payload.get("payload_json", event_payload)
    if not isinstance(payload_data, dict):
        raise InvalidPayloadError("payload_json must be an object")

    handler = EVENT_HANDLERS.get(event.event_type)
    if handler is None:
        _handle_unknown_event(session, event, payload_data)
        return

    handler(session, event, payload_data)
    event.processing_status = WebhookProcessingStatus.processed
    event.processed_at = dt.datetime.utcnow()


def create_subscription(session: Session, subscription_in: SubscriptionCreateIn) -> SubscriptionCreateOut:
    if subscription_in.customer_id is None and subscription_in.customer_email is None:
        raise InvalidPayloadError("customer_id or customer_email is required")

    if subscription_in.customer_id is not None:
        customer = session.get(Customer, subscription_in.customer_id)
        if customer is None:
            raise NotFoundError(f"Customer '{subscription_in.customer_id}' not found")
    else:
        customer = _get_or_create_customer_by_email(session, str(subscription_in.customer_email))

    provider_customer_id = _ensure_provider_customer_id(session, customer)
    provider_subscription_id = f"sub_{uuid.uuid4().hex[:16]}"

    subscription = Subscription(
        customer_id=customer.id,
        plan_id=subscription_in.plan_id,
        status=SubscriptionStatus.pending_activation,
        current_period_end=dt.datetime.utcnow(),
        provider_subscription_id=provider_subscription_id,
    )
    session.add(subscription)
    session.commit()
    session.refresh(subscription)

    return SubscriptionCreateOut(
        subscription_id=subscription.id,
        provider_subscription_id=provider_subscription_id,
        customer_id=customer.id,
        provider_customer_id=provider_customer_id,
        status=subscription.status,
        plan_id=subscription.plan_id,
    )


def _mark_failed(event: WebhookEvent, message: str) -> None:
    event.attempt_count += 1
    retry_delay_seconds = min(300 * event.attempt_count, 3600)
    event.next_retry_at = dt.datetime.utcnow() + dt.timedelta(seconds=retry_delay_seconds)
    event.needs_attention = event.attempt_count >= 3
    event.processing_status = WebhookProcessingStatus.failed
    event.processed_at = dt.datetime.utcnow()
    event.error_message = message
    METRICS["webhook_failed"] += 1
    logger.warning(
        "webhook_failed provider=%s event_id=%s event_type=%s error=%s",
        event.provider,
        event.event_id,
        event.event_type,
        message,
    )


def _get_existing_event(session: Session, provider: str, event_id: str) -> WebhookEvent | None:
    statement = select(WebhookEvent).where(
        WebhookEvent.provider == provider,
        WebhookEvent.event_id == event_id,
    )
    return session.exec(statement).first()


def _handle_existing_event(
    session: Session,
    event: WebhookEvent,
    verified: VerifiedWebhookData,
) -> WebhookEvent:
    if verified.timestamp != event.signature_timestamp:
        _mark_failed(event, "replay timestamp mismatch")
        session.add(event)
        session.commit()
        raise ReplayAttackError("replay timestamp mismatch")
    if verified.signature != event.signature:
        _mark_failed(event, "replay signature mismatch")
        session.add(event)
        session.commit()
        raise ReplayAttackError("replay signature mismatch")

    if event.processing_status in (WebhookProcessingStatus.processed, WebhookProcessingStatus.ignored):
        METRICS["webhook_replayed"] += 1
        return event

    if event.processing_status == WebhookProcessingStatus.failed:
        try:
            dispatch_event(session, event)
            event.next_retry_at = None
            event.needs_attention = False
            event.error_message = None
            session.add(event)
            session.commit()
            session.refresh(event)
            return event
        except InvalidPayloadError as exc:
            _mark_failed(event, str(exc))
            session.add(event)
            session.commit()
            raise
        except Exception as exc:  # pragma: no cover - defensive branch
            _mark_failed(event, str(exc))
            session.add(event)
            session.commit()
            raise
    return event


def process_webhook(session: Session, provider: str, webhook: WebhookEventIn, verified: VerifiedWebhookData) -> WebhookEvent:
    existing = _get_existing_event(session, provider=provider, event_id=webhook.event_id)
    if existing is not None:
        return _handle_existing_event(session, existing, verified)

    event = WebhookEvent(
        provider=provider,
        event_id=webhook.event_id,
        event_type=webhook.event_type,
        payload_raw=verified.raw_body,
        signature=verified.signature,
        signature_timestamp=verified.timestamp,
        attempt_count=1,
        processing_status=WebhookProcessingStatus.received,
    )

    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = _get_existing_event(session, provider=provider, event_id=webhook.event_id)
        if existing is None:
            raise
        return _handle_existing_event(session, existing, verified)

    try:
        dispatch_event(session, event)
        if event.processing_status == WebhookProcessingStatus.ignored:
            METRICS["webhook_ignored"] += 1
        else:
            METRICS["webhook_processed"] += 1
        logger.info(
            "webhook_processed provider=%s event_id=%s event_type=%s status=%s",
            event.provider,
            event.event_id,
            event.event_type,
            event.processing_status,
        )
        event.next_retry_at = None
        event.needs_attention = False
        session.add(event)
        session.commit()
        session.refresh(event)
        return event
    except InvalidPayloadError as exc:
        _mark_failed(event, str(exc))
        session.add(event)
        session.commit()
        raise
    except Exception as exc:  # pragma: no cover - defensive branch
        _mark_failed(event, str(exc))
        session.add(event)
        session.commit()
        raise


def list_webhook_events(session: Session) -> list[WebhookEvent]:
    statement = select(WebhookEvent).order_by(WebhookEvent.id.desc())
    return list(session.exec(statement).all())


def get_webhook_event(session: Session, event_id: str, provider: str | None = None) -> WebhookEvent:
    statement = select(WebhookEvent).where(WebhookEvent.event_id == event_id)
    if provider:
        statement = statement.where(WebhookEvent.provider == provider)
    events = list(session.exec(statement).all())
    if not events:
        raise NotFoundError(f"Webhook '{event_id}' not found")
    if len(events) > 1:
        raise InvalidPayloadError("multiple events found; specify provider")
    return events[0]


def retry_failed_webhooks(session: Session, limit: int = 50) -> dict[str, Any]:
    now = dt.datetime.utcnow()
    statement = (
        select(WebhookEvent)
        .where(
            WebhookEvent.processing_status == WebhookProcessingStatus.failed,
            WebhookEvent.needs_attention == False,  # noqa: E712
            (WebhookEvent.next_retry_at == None) | (WebhookEvent.next_retry_at <= now),  # noqa: E711
        )
        .order_by(WebhookEvent.id.asc())
        .limit(limit)
    )
    events = list(session.exec(statement).all())
    processed_ids: list[int] = []
    failed_ids: list[int] = []
    for event in events:
        try:
            dispatch_event(session, event)
            if event.processing_status == WebhookProcessingStatus.ignored:
                METRICS["webhook_ignored"] += 1
            else:
                METRICS["webhook_processed"] += 1
            event.next_retry_at = None
            event.needs_attention = False
            event.error_message = None
            session.add(event)
            processed_ids.append(event.id)
        except Exception as exc:  # pragma: no cover - defensive branch
            _mark_failed(event, str(exc))
            session.add(event)
            failed_ids.append(event.id)
    session.commit()
    return {
        "checked": len(events),
        "processed_ids": processed_ids,
        "failed_ids": failed_ids,
    }


def reprocess_webhook_event(session: Session, event_id: str) -> WebhookEvent:
    webhook_event = get_webhook_event(session, event_id)
    try:
        dispatch_event(session, webhook_event)
        if webhook_event.processing_status == WebhookProcessingStatus.ignored:
            METRICS["webhook_ignored"] += 1
        else:
            METRICS["webhook_processed"] += 1
        webhook_event.next_retry_at = None
        webhook_event.needs_attention = False
        webhook_event.error_message = None
    except Exception as exc:
        _mark_failed(webhook_event, str(exc))
    session.add(webhook_event)
    session.commit()
    session.refresh(webhook_event)
    return webhook_event


def get_metrics() -> dict[str, int]:
    return dict(METRICS)


def enforce_grace_period(session: Session) -> dict[str, Any]:
    now = dt.datetime.utcnow()
    grace_limit = now - dt.timedelta(days=1)
    canceled_subscriptions: list[int] = []

    statement = select(Subscription).where(Subscription.status == SubscriptionStatus.past_due)
    subscriptions = list(session.exec(statement).all())
    for subscription in subscriptions:
        if subscription.past_due_since is None:
            continue
        if subscription.past_due_since > grace_limit:
            continue

        subscription.status = SubscriptionStatus.canceled
        subscription.canceled_at = now
        subscription.access_revoked = True
        subscription.updated_at = now
        session.add(subscription)
        canceled_subscriptions.append(subscription.id)

    session.commit()
    return {
        "checked_at": now.isoformat(),
        "canceled_count": len(canceled_subscriptions),
        "canceled_subscription_ids": canceled_subscriptions,
    }


def expire_subscriptions(session: Session) -> dict[str, Any]:
    now = dt.datetime.utcnow()
    statement = select(Subscription).where(
        Subscription.status == SubscriptionStatus.active,
        Subscription.current_period_end <= now,
    )
    subscriptions = list(session.exec(statement).all())
    expired_ids: list[int] = []
    canceled_ids: list[int] = []
    for subscription in subscriptions:
        if subscription.cancel_at_period_end:
            subscription.status = SubscriptionStatus.canceled
            subscription.canceled_at = now
            subscription.access_revoked = True
            canceled_ids.append(subscription.id)
        else:
            subscription.status = SubscriptionStatus.expired
            subscription.expired_at = now
            expired_ids.append(subscription.id)
        subscription.updated_at = now
        session.add(subscription)
    session.commit()
    return {
        "checked_at": now.isoformat(),
        "expired_ids": expired_ids,
        "canceled_ids": canceled_ids,
    }


def set_subscription_cancel_at_period_end(
    session: Session,
    subscription_id: int,
    payload: SubscriptionCancelAtPeriodEndIn,
) -> Subscription:
    subscription = session.get(Subscription, subscription_id)
    if subscription is None:
        raise NotFoundError(f"Subscription '{subscription_id}' not found")
    subscription.cancel_at_period_end = payload.cancel_at_period_end
    subscription.updated_at = dt.datetime.utcnow()
    session.add(subscription)
    session.commit()
    session.refresh(subscription)
    return subscription
