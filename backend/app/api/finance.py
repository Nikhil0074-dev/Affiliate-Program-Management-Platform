import hmac
import json
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.security import sign_payload
from ..database import get_db
from ..deps import current_user, org_from_api_key, org_of, staff_user
from ..models import Affiliate, Conversion, LedgerEntry, Order, Organization, Payout, PayoutBatch, User
from ..services import payout as payout_svc
from ..services.audit import audit, notify
from ..services.commission import apply_refund, derived_status
from ..utils import AppError, money, serialize, utcnow

router = APIRouter(tags=["finance"])


def _my_aff_id(db: Session, user: User):
    a = db.scalar(select(Affiliate).where(Affiliate.user_id == user.id))
    return a.id if a else -1


# ---------- ledger / commissions ----------
@router.get("/api/commissions")
def list_commissions(affiliate_id: int | None = None, limit: int = 100, offset: int = 0,
                     user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    q = select(LedgerEntry, Conversion).outerjoin(Conversion, Conversion.id == LedgerEntry.conversion_id).where(
        LedgerEntry.organization_id == org.id)
    if user.role == "AFFILIATE":
        q = q.where(LedgerEntry.affiliate_id == _my_aff_id(db, user))
    elif affiliate_id:
        q = q.where(LedgerEntry.affiliate_id == affiliate_id)
    rows = db.execute(q.order_by(LedgerEntry.id.desc()).limit(min(limit, 500)).offset(max(offset, 0))).all()
    return [serialize(e, status=derived_status(e, c)) for e, c in rows]


@router.get("/api/commissions/{eid}")
def get_commission(eid: int, user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    row = db.execute(select(LedgerEntry, Conversion).outerjoin(Conversion, Conversion.id == LedgerEntry.conversion_id).where(
        LedgerEntry.id == eid, LedgerEntry.organization_id == org.id)).first()
    if not row or (user.role == "AFFILIATE" and row[0].affiliate_id != _my_aff_id(db, user)):
        raise AppError(404, "Ledger entry not found")
    return serialize(row[0], status=derived_status(*row))


class AdjustmentIn(BaseModel):
    affiliate_id: int
    amount: Decimal
    description: str = Field(min_length=3, max_length=500)


@router.post("/api/commissions/adjustments", status_code=201)
def manual_adjustment(body: AdjustmentIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                      db: Session = Depends(get_db)):
    if body.amount == 0:
        raise AppError(422, "Amount must not be zero")
    if not db.scalar(select(Affiliate.id).where(Affiliate.id == body.affiliate_id, Affiliate.organization_id == org.id)):
        raise AppError(404, "Affiliate not found")
    e = LedgerEntry(organization_id=org.id, affiliate_id=body.affiliate_id, type="MANUAL_ADJUSTMENT",
                    amount=money(body.amount), currency=org.currency, available_at=utcnow(), description=body.description)
    db.add(e)
    db.flush()
    audit(db, org.id, user.id, "ledger.manual_adjustment", "ledger", e.id, amount=str(e.amount))
    db.commit()
    return serialize(e)


# ---------- conversions & fraud review ----------
@router.get("/api/conversions")
def list_conversions(flagged: bool | None = None, user: User = Depends(current_user), org: Organization = Depends(org_of),
                     db: Session = Depends(get_db)):
    q = select(Conversion, Order).join(Order, Order.id == Conversion.order_id).where(Order.organization_id == org.id)
    if user.role == "AFFILIATE":
        q = q.where(Conversion.affiliate_id == _my_aff_id(db, user))
    if flagged is True:
        q = q.where(Conversion.flag_status == "FLAGGED")
    rows = db.execute(q.order_by(Conversion.id.desc()).limit(200)).all()
    return [serialize(c, external_order_id=o.external_order_id, eligible_amount=str(o.eligible_amount),
                      order_status=o.status) for c, o in rows]


class ReviewIn(BaseModel):
    decision: Literal["clear", "reject"]
    notes: str | None = None


@router.post("/api/conversions/{cid}/review")
def review_conversion(cid: int, body: ReviewIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                      db: Session = Depends(get_db)):
    row = db.execute(select(Conversion, Order).join(Order, Order.id == Conversion.order_id).where(
        Conversion.id == cid, Order.organization_id == org.id)).first()
    if not row:
        raise AppError(404, "Conversion not found")
    conv, order = row
    if conv.flag_status != "FLAGGED":
        raise AppError(409, "Conversion is not awaiting review")
    conv.review_notes = body.notes
    if body.decision == "clear":
        conv.flag_status = "CLEARED"
    else:
        conv.flag_status = "REJECTED"
        left = money(db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                               .where(LedgerEntry.conversion_id == conv.id)))
        if left > 0:
            db.add(LedgerEntry(organization_id=org.id, affiliate_id=conv.affiliate_id, conversion_id=conv.id,
                               type="MANUAL_ADJUSTMENT", amount=-left, currency=order.currency,
                               available_at=conv.available_at, description="Reversed after fraud review"))
        conv.status = "REVERSED"
    audit(db, org.id, user.id, f"conversion.review_{body.decision}", "conversion", conv.id, notes=body.notes)
    db.commit()
    return serialize(conv)


# ---------- refunds ----------
class RefundIn(BaseModel):
    external_order_id: str
    amount: Decimal | None = Field(default=None, gt=0)
    reason: str | None = None
    kind: Literal["REFUND", "CHARGEBACK"] = "REFUND"


def _do_refund(body: RefundIn, org: Organization, db: Session, user_id=None):
    order = db.scalar(select(Order).where(Order.organization_id == org.id, Order.external_order_id == body.external_order_id))
    if not order:
        raise AppError(404, "Order not found")
    r = apply_refund(db, org, order, body.amount, body.reason, body.kind, user_id)
    return {"refund": serialize(r), "order_status": order.status}


@router.post("/api/refunds", status_code=201)
def create_refund(body: RefundIn, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _do_refund(body, org, db, user.id)


@router.post("/api/webhooks/orders/refund", status_code=201)
def refund_webhook(body: RefundIn, org: Organization = Depends(org_from_api_key), db: Session = Depends(get_db)):
    return _do_refund(body, org, db)


# ---------- payouts ----------
def _batch_out(b: PayoutBatch) -> dict:
    return serialize(b)


def _payout_out(p: Payout, name: str | None = None) -> dict:
    return serialize(p, affiliate_name=name)


@router.get("/api/payouts/batches")
def list_batches(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [_batch_out(b) for b in db.scalars(select(PayoutBatch).where(PayoutBatch.organization_id == org.id)
                                              .order_by(PayoutBatch.id.desc()))]


@router.get("/api/payouts/batches/{bid}")
def get_batch(bid: int, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    b = db.scalar(select(PayoutBatch).where(PayoutBatch.id == bid, PayoutBatch.organization_id == org.id))
    if not b:
        raise AppError(404, "Payout batch not found")
    rows = db.execute(select(Payout, User.name).join(Affiliate, Affiliate.id == Payout.affiliate_id)
                      .join(User, User.id == Affiliate.user_id).where(Payout.payout_batch_id == b.id)).all()
    return {**_batch_out(b), "payouts": [_payout_out(p, n) for p, n in rows]}


@router.post("/api/payouts/generate", status_code=201)
def generate(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _batch_out(payout_svc.generate_batch(db, org, user.id))


@router.post("/api/payouts/batches/{bid}/approve")
def approve(bid: int, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _batch_out(payout_svc.approve_batch(db, org, bid, user.id))


@router.post("/api/payouts/batches/{bid}/process")
def process(bid: int, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _batch_out(payout_svc.process_batch(db, org, bid, user.id))


@router.get("/api/payouts")
def list_payouts(user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    q = select(Payout, User.name).join(Affiliate, Affiliate.id == Payout.affiliate_id).join(User, User.id == Affiliate.user_id) \
        .where(Payout.organization_id == org.id)
    if user.role == "AFFILIATE":
        q = q.where(Payout.affiliate_id == _my_aff_id(db, user))
    return [_payout_out(p, n) for p, n in db.execute(q.order_by(Payout.id.desc()).limit(200)).all()]


@router.post("/api/payouts/{pid}/retry")
def retry(pid: int, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _payout_out(payout_svc.retry_payout(db, org, pid, user.id))


class SimulateIn(BaseModel):
    outcome: Literal["completed", "failed"]


@router.post("/api/payouts/{pid}/simulate")
def simulate(pid: int, body: SimulateIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
             db: Session = Depends(get_db)):
    """Demo helper: behaves exactly like the provider calling the webhook."""
    if not settings.simulate_provider:
        raise AppError(404, "Not found")
    p = db.scalar(select(Payout).where(Payout.id == pid, Payout.organization_id == org.id))
    if not p or not p.provider_reference:
        raise AppError(404, "Payout not found or not processing")
    import uuid
    result = payout_svc.handle_provider_event(db, f"sim_evt_{uuid.uuid4().hex}", f"payout.{body.outcome}", p.provider_reference)
    return {"result": result}


@router.post("/api/webhooks/payment")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    sig = request.headers.get("x-signature", "")
    if not hmac.compare_digest(sign_payload(raw, settings.payment_webhook_secret), sig):
        raise AppError(401, "Invalid webhook signature")
    try:
        data = json.loads(raw)
        event_id, etype, ref = str(data["event_id"]), str(data["type"]), str(data["reference"])
    except (ValueError, KeyError, TypeError):
        raise AppError(400, "Malformed webhook payload")
    return {"result": payout_svc.handle_provider_event(db, event_id, etype, ref)}
