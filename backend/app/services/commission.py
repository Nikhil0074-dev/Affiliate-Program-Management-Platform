from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Affiliate, CommissionRule, CommissionTier, Conversion, LedgerEntry, Order,
                      Organization, Payout, Product, Refund, User)
from ..utils import AppError, money, mstr, utcnow
from . import tracking
from .audit import audit, notify

REVERSAL_TYPES = ("REFUND_REVERSAL", "CHARGEBACK_REVERSAL", "MANUAL_ADJUSTMENT")
OPEN_PAYOUT = ("PENDING_REVIEW", "APPROVED", "PROCESSING")


def month_start(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def find_tier(db: Session, org: Organization, affiliate_id: int, eligible: Decimal, at):
    tiers = db.scalars(select(CommissionTier).where(CommissionTier.organization_id == org.id)
                       .order_by(CommissionTier.min_value)).all()
    if not tiers:
        return None
    base = select(func.count(Conversion.id) if org.commission_mode == "COUNT_TIER"
                  else func.coalesce(func.sum(Order.eligible_amount), 0)) \
        .select_from(Conversion).join(Order, Order.id == Conversion.order_id) \
        .where(Conversion.affiliate_id == affiliate_id, Conversion.status != "REVERSED",
               Order.created_at >= month_start(at))
    prior = Decimal(str(db.scalar(base) or 0))
    value = prior + (Decimal(1) if org.commission_mode == "COUNT_TIER" else eligible)  # includes this sale
    match = None
    for t in tiers:
        if t.min_value <= value and (t.max_value is None or value <= t.max_value):
            return t
        if t.min_value <= value:
            match = t  # gap fallback: highest tier whose minimum has been reached
    return match


def commission_for(db: Session, org: Organization, affiliate: Affiliate, product: Product,
                   eligible: Decimal, at) -> tuple[Decimal, str]:
    """Returns (commission amount, description of the rule that produced it)."""
    if org.commission_mode in ("SALES_TIER", "COUNT_TIER"):
        tier = find_tier(db, org, affiliate.id, eligible, at)
        if tier:
            return money(eligible * tier.commission_rate / 100), f"{tier.name} tier {tier.commission_rate}%"
    rules = db.scalars(select(CommissionRule).where(
        CommissionRule.organization_id == org.id, CommissionRule.active.is_(True),
        (CommissionRule.product_id == product.id) | (CommissionRule.product_id.is_(None)))).all()
    rules.sort(key=lambda r: (r.product_id is not None, r.priority, r.id), reverse=True)
    if rules:
        r = rules[0]
        if r.type == "FIXED":
            return min(money(r.fixed_amount), eligible), f"fixed {mstr(r.fixed_amount)}"
        return money(eligible * r.rate / 100), f"rule {r.rate}%"
    return money(eligible * org.default_rate / 100), f"default {org.default_rate}%"


def fraud_reasons(db: Session, affiliate: Affiliate, aff_user: User, customer: str, click) -> list[str]:
    reasons = []
    if customer.strip().lower() == aff_user.email.lower():
        reasons.append("Possible self-referral")
    now = utcnow()
    repeat = db.scalar(select(func.count(Conversion.id)).join(Order, Order.id == Conversion.order_id).where(
        Conversion.affiliate_id == affiliate.id, Order.customer_reference == customer,
        Order.created_at >= now - timedelta(hours=24))) or 0
    if repeat >= 3:
        reasons.append("Repeated customer identity")
    burst = db.scalar(select(func.count(Conversion.id)).where(
        Conversion.affiliate_id == affiliate.id, Conversion.created_at >= now - timedelta(minutes=10))) or 0
    if burst >= 10:
        reasons.append("Abnormally rapid conversions")
    if click is not None and click.suspicious:
        reasons.append("Suspicious click traffic")
    return reasons


def ingest_order(db: Session, org: Organization, data: dict):
    """Create an order and (when attributable) a conversion + commission. Idempotent on external_order_id."""
    ext = data["external_order_id"]
    existing = db.scalar(select(Order).where(Order.organization_id == org.id, Order.external_order_id == ext))
    if existing:
        conv = db.scalar(select(Conversion).where(Conversion.order_id == existing.id))
        return existing, conv, True
    product = db.scalar(select(Product).where(Product.id == data["product_id"], Product.organization_id == org.id))
    if not product:
        raise AppError(404, "Product not found")
    amount, discount = money(data["amount"]), money(data.get("discount") or 0)
    if discount > amount:
        raise AppError(422, "Discount cannot exceed amount")
    eligible = amount - discount
    now = utcnow()
    order = Order(organization_id=org.id, external_order_id=ext, customer_reference=data["customer_reference"],
                  product_id=product.id, amount=amount, discount=discount, tax=money(data.get("tax") or 0),
                  eligible_amount=eligible, currency=(data.get("currency") or org.currency).upper(), created_at=now)
    db.add(order)
    db.flush()
    attr = tracking.resolve_attribution(db, org, data.get("tracking_id"), data.get("coupon_code"), now)
    if not attr or eligible <= 0:
        audit(db, org.id, None, "order.ingested", "order", order.id, attributed=False)
        db.commit()
        return order, None, False
    affiliate, click, atype = attr
    aff_user = db.get(User, affiliate.user_id)
    amount_c, why = commission_for(db, org, affiliate, product, eligible, now)
    reasons = fraud_reasons(db, affiliate, aff_user, order.customer_reference, click)
    conv = Conversion(order_id=order.id, affiliate_id=affiliate.id, referral_link_id=click.referral_link_id if click else None,
                      click_id=click.id if click else None, attribution_type=atype,
                      attribution_timestamp=click.created_at if click else now,
                      flag_status="FLAGGED" if reasons else "NONE", flag_reason="; ".join(reasons) or None,
                      available_at=now + timedelta(days=org.hold_days))
    db.add(conv)
    db.flush()
    db.add(LedgerEntry(organization_id=org.id, affiliate_id=affiliate.id, conversion_id=conv.id, type="COMMISSION",
                       amount=amount_c, currency=order.currency, available_at=conv.available_at,
                       description=f"Commission on order {ext} ({why})"))
    notify(db, aff_user.id, "commission.earned", f"You earned {order.currency} {amount_c} on order {ext}.")
    audit(db, org.id, None, "conversion.created", "conversion", conv.id, order=ext, commission=str(amount_c),
          flagged=bool(reasons))
    db.commit()
    return order, conv, False


def apply_refund(db: Session, org: Organization, order: Order, amount, reason: str | None, kind: str,
                 user_id: int | None = None) -> Refund:
    if order.organization_id != org.id:
        raise AppError(404, "Order not found")
    done = db.scalar(select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.order_id == order.id)) or 0
    remaining = order.eligible_amount - money(done)
    if remaining <= 0:
        raise AppError(409, "Order is already fully refunded")
    amt = remaining if (amount is None or kind == "CHARGEBACK") else money(amount)
    if amt <= 0 or amt > remaining:
        raise AppError(422, f"Refund amount must be between 0.01 and {remaining}")
    refund = Refund(order_id=order.id, amount=amt, kind=kind, reason=reason)
    db.add(refund)
    final = amt == remaining
    order.status = "CHARGEBACK" if kind == "CHARGEBACK" else ("REFUNDED" if final else "PARTIALLY_REFUNDED")
    conv = db.scalar(select(Conversion).where(Conversion.order_id == order.id))
    if conv:
        total = money(db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.conversion_id == conv.id, LedgerEntry.type == "COMMISSION")))
        reversed_ = -money(db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.conversion_id == conv.id, LedgerEntry.type.in_(REVERSAL_TYPES))))
        rev = total - reversed_ if final else money(total * amt / order.eligible_amount)
        rev = min(rev, total - reversed_)
        if rev > 0:
            db.add(LedgerEntry(organization_id=org.id, affiliate_id=conv.affiliate_id, conversion_id=conv.id,
                               type="CHARGEBACK_REVERSAL" if kind == "CHARGEBACK" else "REFUND_REVERSAL",
                               amount=-rev, currency=order.currency, available_at=conv.available_at,
                               description=f"{kind.title()} on order {order.external_order_id}: {reason or 'n/a'}"))
            aff = db.get(Affiliate, conv.affiliate_id)
            notify(db, aff.user_id, "commission.reversed",
                   f"Commission reduced by {order.currency} {rev} (order {order.external_order_id}).")
        conv.status = "REVERSED" if (total - reversed_ - rev) <= 0 else "PARTIALLY_REVERSED"
    audit(db, org.id, user_id, f"order.{kind.lower()}", "order", order.id, amount=str(amt))
    db.commit()
    return refund


def balances(db: Session, affiliate_id: int) -> dict:
    now = utcnow()
    rows = db.execute(select(LedgerEntry, Conversion.flag_status).outerjoin(
        Conversion, Conversion.id == LedgerEntry.conversion_id).where(LedgerEntry.affiliate_id == affiliate_id)).all()
    earned = reversed_ = payable = pending = on_hold = total = Decimal("0.00")
    for e, flag in rows:
        total += e.amount
        if e.type == "COMMISSION":
            earned += e.amount
        elif e.type in REVERSAL_TYPES:
            reversed_ += -e.amount
        if e.available_at > now:
            if e.type in ("COMMISSION",) + REVERSAL_TYPES:
                pending += e.amount
        elif flag == "FLAGGED":
            on_hold += e.amount
        else:
            payable += e.amount
    paid = money(db.scalar(select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.affiliate_id == affiliate_id, Payout.status == "PAID")))
    in_flight = money(db.scalar(select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.affiliate_id == affiliate_id, Payout.status.in_(OPEN_PAYOUT))))
    return {"total_earned": mstr(earned), "reversed": mstr(reversed_), "paid": mstr(paid),
            "in_payout": mstr(in_flight), "pending": mstr(pending), "on_hold": mstr(on_hold),
            "payable": mstr(payable), "ledger_balance": mstr(total)}


def payable_balance(db: Session, affiliate_id: int) -> Decimal:
    return money(balances(db, affiliate_id)["payable"])


def derived_status(entry: LedgerEntry, conv: Conversion | None) -> str:
    if entry.type == "PAYOUT":
        return "PAID_OUT"
    if entry.type == "PAYOUT_RETURN":
        return "RETURNED"
    if conv is None:
        return "POSTED"
    if conv.status == "REVERSED":
        return "REVERSED"
    if conv.flag_status == "FLAGGED":
        return "VALIDATING"
    return "PENDING" if entry.available_at > utcnow() else "PAYABLE"
