import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlmodel import Field, SQLModel
from sqlalchemy import UniqueConstraint


class SubscriptionStatus(str, Enum):
    active = "active"
    canceled = "canceled"
    past_due = "past_due"
    pending_activation = "pending_activation"
    expired = "expired"


class PaymentStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    refused = "refused"


class WebhookProcessingStatus(str, Enum):
    received = "received"
    processed = "processed"
    failed = "failed"
    ignored = "ignored"


class WebhookEventIn(BaseModel):
    event_id: str
    event_type: str
    payload_json: dict[str, Any]

    model_config = ConfigDict(extra="allow")


class VerifiedWebhookData(BaseModel):
    raw_body: str
    signature: str
    timestamp: int


class SubscriptionCreateIn(BaseModel):
    customer_id: int | None = None
    customer_email: str | None = None
    plan_id: int


class SubscriptionCreateOut(BaseModel):
    subscription_id: int
    provider_subscription_id: str
    customer_id: int
    provider_customer_id: str
    status: SubscriptionStatus
    plan_id: int


class SubscriptionCancelAtPeriodEndIn(BaseModel):
    cancel_at_period_end: bool = True


class PaymentSucceededPayload(BaseModel):
    provider_customer_id: str
    provider_subscription_id: str
    amount: int = 0
    currency: str = "USD"
    current_period_end: str | int | float | None = None
    payment_id: str | None = None
    invoice_id: str | None = None


class InvoicePaymentFailedPayload(BaseModel):
    provider_customer_id: str
    provider_subscription_id: str
    amount: int = 0
    currency: str = "USD"
    current_period_end: str | int | float | None = None
    payment_id: str | None = None
    invoice_id: str | None = None


class Customer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    provider_customer_id: str | None = Field(default=None, index=True, unique=True)
    email: str = Field(index=True, unique=True)
    status: str | None = None
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class Subscription(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id")
    plan_id: int
    status: SubscriptionStatus
    current_period_end: dt.datetime
    cancel_at_period_end: bool = False
    past_due_since: dt.datetime | None = None
    canceled_at: dt.datetime | None = None
    expired_at: dt.datetime | None = None
    provider_subscription_id: str = Field(index=True, unique=True)
    access_revoked: bool = False
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class Payment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id")
    subscription_id: int = Field(foreign_key="subscription.id")
    status: PaymentStatus
    amount: int
    currency: str
    provider_payment_id: str
    provider_invoice_id: str
    processed_at: dt.datetime | None = None
    provider: str


class WebhookEvent(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)

    id: int | None = Field(default=None, primary_key=True)
    provider: str
    event_id: str = Field(index=True)
    event_type: str
    payload_raw: str
    signature: str
    signature_timestamp: int
    received_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    processed_at: dt.datetime | None = None
    attempt_count: int = 0
    next_retry_at: dt.datetime | None = None
    needs_attention: bool = False
    processing_status: WebhookProcessingStatus = WebhookProcessingStatus.received
    error_message: str | None = None


class InvalidPayloadError(ValueError):
    pass


class NotFoundError(ValueError):
    pass


class ReplayAttackError(ValueError):
    pass
