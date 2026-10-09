from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import my_affiliate, org_of, platform_admin, staff_user
from ..models import (Affiliate, AuditLog, Campaign, Click, Conversion, LedgerEntry, Order, Organization, Payout,
                      Product, ReferralLink, User)
from ..services.commission import OPEN_PAYOUT, balances
from ..utils import mstr, serialize

router = APIRouter(tags=["analytics"])


def _sum(db, expr, *where):
    return Decimal(str(db.scalar(select(func.coalesce(func.sum(expr), 0)).where(*where)) or 0))


def _core(db: Session, aff_ids: list[int]) -> dict:
    clicks = db.scalar(select(func.count(Click.id)).where(Click.affiliate_id.in_(aff_ids))) or 0
    convs = db.scalar(select(func.count(Conversion.id)).where(Conversion.affiliate_id.in_(aff_ids))) or 0
    revenue = Decimal(str(db.scalar(select(func.coalesce(func.sum(Order.eligible_amount), 0)).select_from(Conversion)
                                    .join(Order, Order.id == Conversion.order_id).where(Conversion.affiliate_id.in_(aff_ids))) or 0))
    earned = _sum(db, LedgerEntry.amount, LedgerEntry.affiliate_id.in_(aff_ids), LedgerEntry.type == "COMMISSION")
    rate = (Decimal(convs) / Decimal(clicks) * 100) if clicks else Decimal(0)
    return {"clicks": clicks, "conversions": convs, "conversion_rate": f"{rate:.2f}", "revenue": mstr(revenue),
            "commission_earned": mstr(earned),
            "average_order_value": mstr(revenue / convs) if convs else "0.00"}


def _top_products(db, aff_ids):
    rows = db.execute(select(Product.name, func.count(Conversion.id), func.coalesce(func.sum(Order.eligible_amount), 0))
                      .select_from(Conversion).join(Order, Order.id == Conversion.order_id)
                      .join(Product, Product.id == Order.product_id).where(Conversion.affiliate_id.in_(aff_ids))
                      .group_by(Product.id, Product.name).order_by(func.sum(Order.eligible_amount).desc()).limit(5)).all()
    return [{"product": n, "conversions": c, "revenue": mstr(r)} for n, c, r in rows]


def _campaigns(db, aff_ids):
    clicks = dict(db.execute(select(ReferralLink.campaign_id, func.count(Click.id)).select_from(Click)
                             .join(ReferralLink, ReferralLink.id == Click.referral_link_id)
                             .where(Click.affiliate_id.in_(aff_ids), ReferralLink.campaign_id.isnot(None))
                             .group_by(ReferralLink.campaign_id)).all())
    rows = db.execute(select(Campaign.id, Campaign.name, func.count(Conversion.id), func.coalesce(func.sum(Order.eligible_amount), 0))
                      .select_from(Conversion).join(Order, Order.id == Conversion.order_id)
                      .join(ReferralLink, ReferralLink.id == Conversion.referral_link_id)
                      .join(Campaign, Campaign.id == ReferralLink.campaign_id)
                      .where(Conversion.affiliate_id.in_(aff_ids)).group_by(Campaign.id, Campaign.name)).all()
    seen = {cid for cid, *_ in rows}
    out = [{"campaign": n, "clicks": clicks.get(cid, 0), "conversions": c, "revenue": mstr(r)} for cid, n, c, r in rows]
    for cid, n in clicks.items():  # campaigns with clicks but no sales yet
        if cid not in seen:
            out.append({"campaign": db.get(Campaign, cid).name, "clicks": n, "conversions": 0, "revenue": "0.00"})
    return sorted(out, key=lambda x: Decimal(x["revenue"]), reverse=True)


@router.get("/api/analytics/affiliate")
def affiliate_dashboard(aff: Affiliate = Depends(my_affiliate), db: Session = Depends(get_db)):
    ids = [aff.id]
    return {**_core(db, ids), "balance": balances(db, aff.id), "top_products": _top_products(db, ids),
            "top_campaigns": _campaigns(db, ids)}


@router.get("/api/analytics/business")
def business_dashboard(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    ids = list(db.scalars(select(Affiliate.id).where(Affiliate.organization_id == org.id)))
    total_orders = db.scalar(select(func.count(Order.id)).where(Order.organization_id == org.id)) or 0
    refunded = db.scalar(select(func.count(Order.id)).where(Order.organization_id == org.id, Order.status != "PAID")) or 0
    pend = _sum(db, Payout.amount, Payout.organization_id == org.id, Payout.status.in_(OPEN_PAYOUT))
    done = _sum(db, Payout.amount, Payout.organization_id == org.id, Payout.status == "PAID")
    top = db.execute(select(User.name, func.sum(LedgerEntry.amount)).select_from(LedgerEntry)
                     .join(Affiliate, Affiliate.id == LedgerEntry.affiliate_id).join(User, User.id == Affiliate.user_id)
                     .where(LedgerEntry.organization_id == org.id, LedgerEntry.type == "COMMISSION")
                     .group_by(Affiliate.id, User.name).order_by(func.sum(LedgerEntry.amount).desc()).limit(5)).all()
    return {**_core(db, ids), "total_affiliates": len(ids),
            "active_affiliates": db.scalar(select(func.count(Affiliate.id)).where(Affiliate.organization_id == org.id, Affiliate.status == "APPROVED")) or 0,
            "pending_applications": db.scalar(select(func.count(Affiliate.id)).where(Affiliate.organization_id == org.id, Affiliate.status.in_(["PENDING", "UNDER_REVIEW"]))) or 0,
            "flagged_conversions": db.scalar(select(func.count(Conversion.id)).join(Order, Order.id == Conversion.order_id).where(Order.organization_id == org.id, Conversion.flag_status == "FLAGGED")) or 0,
            "reversed": mstr(-_sum(db, LedgerEntry.amount, LedgerEntry.organization_id == org.id,
                                   LedgerEntry.type.in_(["REFUND_REVERSAL", "CHARGEBACK_REVERSAL"]))),
            "pending_payouts": mstr(pend), "completed_payouts": mstr(done),
            "refund_rate": f"{(Decimal(refunded) / Decimal(total_orders) * 100) if total_orders else Decimal(0):.2f}",
            "top_affiliates": [{"name": n, "commission": mstr(c)} for n, c in top],
            "top_products": _top_products(db, ids), "campaigns": _campaigns(db, ids)}


# ---------- platform administration ----------
@router.get("/api/admin/organizations")
def admin_orgs(_: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return [serialize(o, exclude=("api_key",)) for o in db.scalars(select(Organization).order_by(Organization.id))]


@router.get("/api/admin/audit-logs")
def admin_audit(limit: int = 200, _: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return [serialize(a) for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))]


@router.get("/api/audit-logs")
def org_audit(limit: int = 200, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(a) for a in db.scalars(select(AuditLog).where(AuditLog.organization_id == org.id)
                                             .order_by(AuditLog.id.desc()).limit(min(limit, 1000)))]
