from datetime import datetime
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .utils import utcnow

Money = Numeric(14, 2)
Rate = Numeric(6, 2)  # percentage, e.g. 20.00 == 20%


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    attribution_days: Mapped[int] = mapped_column(Integer, default=30)
    hold_days: Mapped[int] = mapped_column(Integer, default=30)
    min_payout: Mapped[Decimal] = mapped_column(Money, default=Decimal("1000.00"))
    default_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("10.00"))
    # RULES | SALES_TIER | COUNT_TIER
    commission_mode: Mapped[str] = mapped_column(String(20), default="RULES")
    terms_version: Mapped[str] = mapped_column(String(20), default="v1")
    api_key: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))  # OWNER | FINANCE | AFFILIATE | PLATFORM_ADMIN
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Affiliate(Base):
    __tablename__ = "affiliates"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    # PENDING | UNDER_REVIEW | APPROVED | REJECTED | SUSPENDED
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    social_links: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    marketing_channels: Mapped[str | None] = mapped_column(String(300), nullable=True)
    country: Mapped[str | None] = mapped_column(String(60), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    base_url: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CommissionRule(Base):
    __tablename__ = "commission_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(10), default="PERCENT")  # PERCENT | FIXED
    rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.00"))
    fixed_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CommissionTier(Base):
    __tablename__ = "commission_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    min_value: Mapped[Decimal] = mapped_column(Money)  # monthly sales amount, or monthly sales count
    max_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    commission_rate: Mapped[Decimal] = mapped_column(Rate)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    medium: Mapped[str | None] = mapped_column(String(60), nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReferralLink(Base):
    __tablename__ = "referral_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    destination_url: Mapped[str] = mapped_column(String(800))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Click(Base):
    __tablename__ = "clicks"
    id: Mapped[int] = mapped_column(primary_key=True)
    referral_link_id: Mapped[int | None] = mapped_column(ForeignKey("referral_links.id"), nullable=True, index=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    tracking_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    landing_page: Mapped[str | None] = mapped_column(String(800), nullable=True)
    referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    device_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    browser: Mapped[str | None] = mapped_column(String(30), nullable=True)
    country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # hashed; raw IP never stored
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"))
    code: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("organization_id", "external_order_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    external_order_id: Mapped[str] = mapped_column(String(100))
    customer_reference: Mapped[str] = mapped_column(String(254))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    eligible_amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    # PAID | PARTIALLY_REFUNDED | REFUNDED | CHARGEBACK
    status: Mapped[str] = mapped_column(String(20), default="PAID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Conversion(Base):
    __tablename__ = "conversions"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"), index=True)
    referral_link_id: Mapped[int | None] = mapped_column(ForeignKey("referral_links.id"), nullable=True)
    click_id: Mapped[int | None] = mapped_column(ForeignKey("clicks.id"), nullable=True)
    attribution_type: Mapped[str] = mapped_column(String(20))  # LAST_CLICK | COUPON
    attribution_timestamp: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | PARTIALLY_REVERSED | REVERSED
    flag_status: Mapped[str] = mapped_column(String(10), default="NONE")  # NONE | FLAGGED | CLEARED | REJECTED
    flag_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime)  # end of holding period
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LedgerEntry(Base):
    """Append-only. Rows are never updated or deleted; balances are always derived."""
    __tablename__ = "commission_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"), index=True)
    conversion_id: Mapped[int | None] = mapped_column(ForeignKey("conversions.id"), nullable=True, index=True)
    payout_id: Mapped[int | None] = mapped_column(ForeignKey("payouts.id"), nullable=True)
    # COMMISSION | REFUND_REVERSAL | CHARGEBACK_REVERSAL | MANUAL_ADJUSTMENT | PAYOUT | PAYOUT_RETURN
    type: Mapped[str] = mapped_column(String(25))
    amount: Mapped[Decimal] = mapped_column(Money)  # signed
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PayoutBatch(Base):
    __tablename__ = "payout_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    affiliate_count: Mapped[int] = mapped_column(Integer, default=0)
    on_hold_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    # PENDING_REVIEW | APPROVED | PROCESSING | PAID | PARTIALLY_FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_REVIEW")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    payout_batch_id: Mapped[int] = mapped_column(ForeignKey("payout_batches.id"), index=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    # PENDING_REVIEW | APPROVED | PROCESSING | PAID | FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_REVIEW")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(12), default="REFUND")  # REFUND | CHARGEBACK
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="PROCESSED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True)
    event_type: Mapped[str] = mapped_column(String(50))
    result: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(500))
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
