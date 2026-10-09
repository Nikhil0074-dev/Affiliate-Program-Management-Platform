import json
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Affiliate, LedgerEntry, Organization, Payout, PayoutBatch, WebhookEvent
from ..utils import AppError, money, mstr, utcnow
from .audit import audit, notify, notify_staff
from .commission import payable_balance


def generate_batch(db: Session, org: Organization, user_id: int | None = None) -> PayoutBatch:
    now = utcnow()
    last = db.scalar(select(PayoutBatch).where(PayoutBatch.organization_id == org.id).order_by(PayoutBatch.id.desc()))
    batch = PayoutBatch(organization_id=org.id, period_start=last.period_end if last else None, period_end=now)
    db.add(batch)
    db.flush()
    total, count, held = Decimal("0.00"), 0, Decimal("0.00")
    for aff in db.scalars(select(Affiliate).where(Affiliate.organization_id == org.id).order_by(Affiliate.id)):
        bal = payable_balance(db, aff.id)
        if bal <= 0:
            continue
        if aff.status != "APPROVED" or bal < org.min_payout:
            held += bal
            continue
        p = Payout(organization_id=org.id, payout_batch_id=batch.id, affiliate_id=aff.id, amount=bal, currency=org.currency)
        db.add(p)
        db.flush()
        # Reserve funds immediately so the same commission can never be paid twice.
        db.add(LedgerEntry(organization_id=org.id, affiliate_id=aff.id, payout_id=p.id, type="PAYOUT", amount=-bal,
                           currency=org.currency, available_at=now, description=f"Payout #{p.id} (batch #{batch.id})"))
        total += bal
        count += 1
    if count == 0:
        db.rollback()
        raise AppError(409, "No affiliates are eligible for payout")
    batch.total_amount, batch.affiliate_count, batch.on_hold_amount = total, count, held
    audit(db, org.id, user_id, "payout_batch.generated", "payout_batch", batch.id, total=str(total), affiliates=count)
    notify_staff(db, org.id, "payout.batch_ready", f"Payout batch #{batch.id} ({org.currency} {total}) awaits review.")
    db.commit()
    return batch


def _get_batch(db, org_id, batch_id) -> PayoutBatch:
    b = db.scalar(select(PayoutBatch).where(PayoutBatch.id == batch_id, PayoutBatch.organization_id == org_id))
    if not b:
        raise AppError(404, "Payout batch not found")
    return b


def approve_batch(db: Session, org: Organization, batch_id: int, user_id: int) -> PayoutBatch:
    b = _get_batch(db, org.id, batch_id)
    if b.status != "PENDING_REVIEW":
        raise AppError(409, f"Batch is {b.status}, expected PENDING_REVIEW")
    for p in db.scalars(select(Payout).where(Payout.payout_batch_id == b.id)):
        p.status = "APPROVED"
    b.status = "APPROVED"
    audit(db, org.id, user_id, "payout_batch.approved", "payout_batch", b.id)
    db.commit()
    return b


def _dispatch(db: Session, p: Payout) -> None:
    p.status, p.provider = "PROCESSING", "SIMULATED"
    p.provider_reference = f"sim_{uuid.uuid4().hex[:16]}"  # real provider: store its payout id here


def process_batch(db: Session, org: Organization, batch_id: int, user_id: int) -> PayoutBatch:
    b = _get_batch(db, org.id, batch_id)
    if b.status != "APPROVED":
        raise AppError(409, f"Batch is {b.status}, expected APPROVED")
    for p in db.scalars(select(Payout).where(Payout.payout_batch_id == b.id, Payout.status == "APPROVED")):
        _dispatch(db, p)
    b.status = "PROCESSING"
    audit(db, org.id, user_id, "payout_batch.processing", "payout_batch", b.id)
    db.commit()
    return b


def retry_payout(db: Session, org: Organization, payout_id: int, user_id: int) -> Payout:
    p = db.scalar(select(Payout).where(Payout.id == payout_id, Payout.organization_id == org.id))
    if not p:
        raise AppError(404, "Payout not found")
    if p.status != "FAILED":
        raise AppError(409, "Only FAILED payouts can be retried")
    if payable_balance(db, p.affiliate_id) < p.amount:
        raise AppError(409, "Affiliate balance no longer covers this payout; review manually")
    db.add(LedgerEntry(organization_id=org.id, affiliate_id=p.affiliate_id, payout_id=p.id, type="PAYOUT",
                       amount=-p.amount, currency=p.currency, available_at=utcnow(),
                       description=f"Payout #{p.id} retry"))
    _dispatch(db, p)
    batch = db.get(PayoutBatch, p.payout_batch_id)
    batch.status = "PROCESSING"
    audit(db, org.id, user_id, "payout.retried", "payout", p.id)
    db.commit()
    return p


def _refresh_batch(db: Session, batch_id: int) -> None:
    db.flush()  # autoflush is off; make pending payout status changes visible
    statuses = {s for s in db.scalars(select(Payout.status).where(Payout.payout_batch_id == batch_id))}
    batch = db.get(PayoutBatch, batch_id)
    if statuses & {"PROCESSING", "APPROVED", "PENDING_REVIEW"}:
        return
    batch.status = "PAID" if statuses == {"PAID"} else "PARTIALLY_FAILED"


def handle_provider_event(db: Session, event_id: str, event_type: str, reference: str) -> str:
    """Idempotent: a repeated event_id is acknowledged but never re-applied."""
    if db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id)):
        return "duplicate"
    p = db.scalar(select(Payout).where(Payout.provider_reference == reference))
    result = "ignored"
    if p and p.status == "PROCESSING":
        from ..models import User
        user = db.get(User, db.get(Affiliate, p.affiliate_id).user_id)
        if event_type == "payout.completed":
            p.status, p.processed_at, result = "PAID", utcnow(), "paid"
            notify(db, user.id, "payout.completed", f"Your payout of {p.currency} {mstr(p.amount)} has been processed.")
        elif event_type == "payout.failed":
            p.status, result = "FAILED", "failed"
            db.add(LedgerEntry(organization_id=p.organization_id, affiliate_id=p.affiliate_id, payout_id=p.id,
                               type="PAYOUT_RETURN", amount=p.amount, currency=p.currency, available_at=utcnow(),
                               description=f"Payout #{p.id} failed - funds returned"))
            notify(db, user.id, "payout.failed", "Your payout could not be completed. Please update your payment details.")
        if result != "ignored":
            _refresh_batch(db, p.payout_batch_id)
            audit(db, p.organization_id, None, f"webhook.{event_type}", "payout", p.id, event_id=event_id)
    elif event_type == "payout.processing" and p:
        result = "noop"
    db.add(WebhookEvent(event_id=event_id, event_type=event_type, result=result))
    db.commit()
    return result
