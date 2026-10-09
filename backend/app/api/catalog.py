import secrets
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user, org_of, owner_user, staff_user
from ..models import (Affiliate, Campaign, CommissionRule, CommissionTier, Coupon, Organization, Product, User)
from ..services.audit import audit
from ..utils import AppError, serialize

router = APIRouter(prefix="/api", tags=["catalog"])


# ---------- organization settings ----------
class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2)
    attribution_days: int | None = Field(default=None, ge=0, le=365)
    hold_days: int | None = Field(default=None, ge=0, le=365)
    min_payout: Decimal | None = Field(default=None, ge=0)
    default_rate: Decimal | None = Field(default=None, ge=0, le=100)
    commission_mode: Literal["RULES", "SALES_TIER", "COUNT_TIER"] | None = None
    terms_version: str | None = None


@router.get("/organization")
def get_org(user: User = Depends(staff_user), org: Organization = Depends(org_of)):
    return serialize(org, exclude=() if user.role == "OWNER" else ("api_key",))


@router.patch("/organization")
def patch_org(body: OrgPatch, user: User = Depends(owner_user), org: Organization = Depends(org_of),
              db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(org, k, v)
    audit(db, org.id, user.id, "organization.updated", "organization", org.id, **body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return serialize(org)


@router.post("/organization/rotate-api-key")
def rotate_key(user: User = Depends(owner_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    org.api_key = "ak_" + secrets.token_urlsafe(24)
    audit(db, org.id, user.id, "organization.api_key_rotated", "organization", org.id)
    db.commit()
    return {"api_key": org.api_key}


# ---------- products ----------
class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    price: Decimal = Field(default=Decimal("0"), ge=0)
    base_url: AnyHttpUrl
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"


class ProductPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    price: Decimal | None = Field(default=None, ge=0)
    base_url: AnyHttpUrl | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


@router.get("/products")
def list_products(user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(p) for p in db.scalars(select(Product).where(Product.organization_id == org.id).order_by(Product.id))]


@router.post("/products", status_code=201)
def create_product(body: ProductIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                   db: Session = Depends(get_db)):
    p = Product(organization_id=org.id, name=body.name, description=body.description, price=body.price,
                currency=org.currency, base_url=str(body.base_url), status=body.status)
    db.add(p)
    db.flush()
    audit(db, org.id, user.id, "product.created", "product", p.id)
    db.commit()
    return serialize(p)


@router.patch("/products/{pid}")
def patch_product(pid: int, body: ProductPatch, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                  db: Session = Depends(get_db)):
    p = db.scalar(select(Product).where(Product.id == pid, Product.organization_id == org.id))
    if not p:
        raise AppError(404, "Product not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(p, k, str(v) if k == "base_url" else v)
    audit(db, org.id, user.id, "product.updated", "product", p.id)
    db.commit()
    return serialize(p)


# ---------- commission rules ----------
class RuleIn(BaseModel):
    product_id: int | None = None
    type: Literal["PERCENT", "FIXED"] = "PERCENT"
    rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    fixed_amount: Decimal = Field(default=Decimal("0"), ge=0)
    priority: int = 0
    active: bool = True


@router.get("/commission-rules")
def list_rules(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(r) for r in db.scalars(select(CommissionRule).where(CommissionRule.organization_id == org.id))]


@router.post("/commission-rules", status_code=201)
def create_rule(body: RuleIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                db: Session = Depends(get_db)):
    if body.product_id and not db.scalar(select(Product.id).where(Product.id == body.product_id, Product.organization_id == org.id)):
        raise AppError(404, "Product not found")
    r = CommissionRule(organization_id=org.id, **body.model_dump())
    db.add(r)
    db.flush()
    audit(db, org.id, user.id, "commission_rule.created", "commission_rule", r.id)
    db.commit()
    return serialize(r)


@router.patch("/commission-rules/{rid}")
def patch_rule(rid: int, body: dict, user: User = Depends(staff_user), org: Organization = Depends(org_of),
               db: Session = Depends(get_db)):
    r = db.scalar(select(CommissionRule).where(CommissionRule.id == rid, CommissionRule.organization_id == org.id))
    if not r:
        raise AppError(404, "Rule not found")
    allowed = {"rate": (Decimal, 0, 100), "fixed_amount": (Decimal, 0, None), "priority": (int, None, None), "active": (bool, None, None)}
    for k, v in body.items():
        if k not in allowed:
            raise AppError(422, f"Field '{k}' cannot be updated")
        typ, lo, hi = allowed[k]
        try:
            val = typ(str(v)) if typ is Decimal else (typ(v) if typ is not bool else bool(v))
        except Exception:
            raise AppError(422, f"Invalid value for {k}")
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            raise AppError(422, f"Value out of range for {k}")
        setattr(r, k, val)
    audit(db, org.id, user.id, "commission_rule.updated", "commission_rule", r.id)
    db.commit()
    return serialize(r)


# ---------- tiers ----------
class TierIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    min_value: Decimal = Field(ge=0)
    max_value: Decimal | None = Field(default=None, ge=0)
    commission_rate: Decimal = Field(ge=0, le=100)


@router.get("/commission-tiers")
def list_tiers(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(t) for t in db.scalars(select(CommissionTier).where(CommissionTier.organization_id == org.id)
                                             .order_by(CommissionTier.min_value))]


@router.post("/commission-tiers", status_code=201)
def create_tier(body: TierIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                db: Session = Depends(get_db)):
    if body.max_value is not None and body.max_value < body.min_value:
        raise AppError(422, "max_value must be >= min_value")
    t = CommissionTier(organization_id=org.id, **body.model_dump())
    db.add(t)
    db.flush()
    audit(db, org.id, user.id, "commission_tier.created", "commission_tier", t.id)
    db.commit()
    return serialize(t)


@router.delete("/commission-tiers/{tid}", status_code=204)
def delete_tier(tid: int, user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    t = db.scalar(select(CommissionTier).where(CommissionTier.id == tid, CommissionTier.organization_id == org.id))
    if not t:
        raise AppError(404, "Tier not found")
    db.delete(t)
    audit(db, org.id, user.id, "commission_tier.deleted", "commission_tier", tid)
    db.commit()


# ---------- campaigns & coupons ----------
class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source: str | None = None
    medium: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None


@router.get("/campaigns")
def list_campaigns(user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(c) for c in db.scalars(select(Campaign).where(Campaign.organization_id == org.id).order_by(Campaign.id))]


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                    db: Session = Depends(get_db)):
    c = Campaign(organization_id=org.id, **body.model_dump())
    db.add(c)
    db.flush()
    audit(db, org.id, user.id, "campaign.created", "campaign", c.id)
    db.commit()
    return serialize(c)


class CouponIn(BaseModel):
    affiliate_id: int
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/coupons")
def list_coupons(user: User = Depends(staff_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return [serialize(c) for c in db.scalars(select(Coupon).where(Coupon.organization_id == org.id))]


@router.post("/coupons", status_code=201)
def create_coupon(body: CouponIn, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                  db: Session = Depends(get_db)):
    if not db.scalar(select(Affiliate.id).where(Affiliate.id == body.affiliate_id, Affiliate.organization_id == org.id)):
        raise AppError(404, "Affiliate not found")
    code = body.code.upper()
    if db.scalar(select(Coupon.id).where(Coupon.organization_id == org.id, Coupon.code == code)):
        raise AppError(409, "Coupon code already exists")
    c = Coupon(organization_id=org.id, affiliate_id=body.affiliate_id, code=code)
    db.add(c)
    db.flush()
    audit(db, org.id, user.id, "coupon.created", "coupon", c.id)
    db.commit()
    return serialize(c)
