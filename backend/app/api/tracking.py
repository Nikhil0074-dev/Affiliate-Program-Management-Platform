import secrets
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import settings
from ..database import get_db
from ..deps import current_user, my_affiliate, org_of, org_from_api_key
from ..models import Affiliate, Campaign, Organization, Product, ReferralLink, User
from ..services import tracking
from ..services.audit import audit
from ..utils import AppError, serialize

router = APIRouter(tags=["tracking"])


def _with_params(url: str, extra: dict) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q.update({k: v for k, v in extra.items() if v})
    return urlunparse(p._replace(query=urlencode(q)))


# ---------- referral links ----------
class LinkIn(BaseModel):
    product_id: int
    campaign_id: int | None = None
    affiliate_id: int | None = None  # staff only


def _link_out(l: ReferralLink) -> dict:
    return serialize(l, url=f"{settings.public_base_url}/r/{l.code}")


@router.get("/api/referral-links")
def list_links(user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    q = select(ReferralLink).join(Affiliate, Affiliate.id == ReferralLink.affiliate_id).where(Affiliate.organization_id == org.id)
    if user.role == "AFFILIATE":
        q = q.where(Affiliate.user_id == user.id)
    return [_link_out(l) for l in db.scalars(q.order_by(ReferralLink.id.desc()))]


@router.post("/api/referral-links", status_code=201)
def create_link(body: LinkIn, user: User = Depends(current_user), org: Organization = Depends(org_of),
                db: Session = Depends(get_db)):
    if user.role == "AFFILIATE":
        aff = db.scalar(select(Affiliate).where(Affiliate.user_id == user.id))
    elif user.role in ("OWNER", "FINANCE") and body.affiliate_id:
        aff = db.scalar(select(Affiliate).where(Affiliate.id == body.affiliate_id, Affiliate.organization_id == org.id))
    else:
        raise AppError(403, "Affiliate access required")
    if not aff:
        raise AppError(404, "Affiliate not found")
    if aff.status != "APPROVED":
        raise AppError(403, "Only approved affiliates can create referral links")
    product = db.scalar(select(Product).where(Product.id == body.product_id, Product.organization_id == org.id,
                                               Product.status == "ACTIVE"))
    if not product:
        raise AppError(404, "Product not found")
    if body.campaign_id and not db.scalar(select(Campaign.id).where(Campaign.id == body.campaign_id,
                                                                    Campaign.organization_id == org.id)):
        raise AppError(404, "Campaign not found")
    link = ReferralLink(affiliate_id=aff.id, product_id=product.id, campaign_id=body.campaign_id,
                        code=f"{aff.code}-{secrets.token_hex(3)}", destination_url=product.base_url)
    db.add(link)
    db.flush()
    audit(db, org.id, user.id, "referral_link.created", "referral_link", link.id)
    db.commit()
    return _link_out(link)


@router.get("/api/referral-links/{lid}")
def get_link(lid: int, user: User = Depends(current_user), org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    q = select(ReferralLink).join(Affiliate, Affiliate.id == ReferralLink.affiliate_id).where(
        ReferralLink.id == lid, Affiliate.organization_id == org.id)
    if user.role == "AFFILIATE":
        q = q.where(Affiliate.user_id == user.id)
    link = db.scalar(q)
    if not link:
        raise AppError(404, "Referral link not found")
    return _link_out(link)


# ---------- click tracking & redirect ----------
@router.get("/r/{code}")
def track_click(code: str, request: Request, product_id: int | None = None, db: Session = Depends(get_db)):
    link = db.scalar(select(ReferralLink).where(ReferralLink.code == code))
    if link:
        aff = db.get(Affiliate, link.affiliate_id)
        product = db.get(Product, link.product_id)
    else:
        aff = db.scalar(select(Affiliate).where(Affiliate.code == code))
        if not aff:
            raise AppError(404, "Unknown referral code")
        q = select(Product).where(Product.organization_id == aff.organization_id, Product.status == "ACTIVE")
        product = db.scalar(q.where(Product.id == product_id) if product_id else q.order_by(Product.id))
    if not aff or aff.status != "APPROVED" or not product:
        raise AppError(404, "Referral link is not active")
    ua = request.headers.get("user-agent", "")
    ip = request.client.host if request.client else None
    campaign = db.get(Campaign, link.campaign_id) if link and link.campaign_id else None
    params = {"ref": aff.code, "campaign": campaign.name if campaign else None,
              "source": campaign.source if campaign else None, "medium": campaign.medium if campaign else None}
    device, _ = tracking.parse_device(ua)
    if device == "bot":  # never count crawlers
        return RedirectResponse(_with_params(product.base_url, params), status_code=302)
    click = tracking.record_click(db, aff, link, product.id, str(request.url), request.headers.get("referer"), ua, ip,
                                  request.headers.get("cf-ipcountry"))
    db.commit()
    org = db.get(Organization, aff.organization_id)
    resp = RedirectResponse(_with_params(product.base_url, {**params, "tid": click.tracking_id}), status_code=302)
    resp.set_cookie("aff_tid", click.tracking_id, max_age=org.attribution_days * 86400, httponly=True, samesite="lax")
    return resp


# ---------- conversion ingestion ----------
class OrderIn(BaseModel):
    external_order_id: str = Field(min_length=1, max_length=100)
    customer_reference: str = Field(min_length=1, max_length=254)
    product_id: int
    amount: Decimal = Field(gt=0)
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    tracking_id: str | None = None
    coupon_code: str | None = None


def _ingest(body: OrderIn, org: Organization, db: Session):
    from ..services.commission import ingest_order
    order, conv, dup = ingest_order(db, org, body.model_dump())
    return {"duplicate": dup, "order": serialize(order),
            "conversion": serialize(conv) if conv else None}


@router.post("/api/tracking/conversion")
def conversion(body: OrderIn, org: Organization = Depends(org_from_api_key), db: Session = Depends(get_db)):
    return _ingest(body, org, db)


@router.post("/api/webhooks/orders")
def order_webhook(body: OrderIn, org: Organization = Depends(org_from_api_key), db: Session = Depends(get_db)):
    return _ingest(body, org, db)
