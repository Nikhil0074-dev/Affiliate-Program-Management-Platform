import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import settings
from ..models import Affiliate, Click, Coupon, Organization, ReferralLink
from ..utils import utcnow


def parse_device(ua: str) -> tuple[str, str]:
    u = (ua or "").lower()
    if any(b in u for b in ("bot", "crawler", "spider", "curl", "python-requests", "headless")):
        device = "bot"
    elif "ipad" in u or "tablet" in u:
        device = "tablet"
    elif "mobi" in u or "android" in u or "iphone" in u:
        device = "mobile"
    else:
        device = "desktop"
    if "edg" in u:
        browser = "Edge"
    elif "firefox" in u:
        browser = "Firefox"
    elif "chrome" in u:
        browser = "Chrome"
    elif "safari" in u:
        browser = "Safari"
    else:
        browser = "Other"
    return device, browser


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(f"{settings.secret_key}:{ip}".encode()).hexdigest()


def record_click(db: Session, affiliate: Affiliate, link: ReferralLink | None, product_id: int | None,
                 landing: str, referrer: str | None, ua: str, ip: str | None, country: str | None) -> Click:
    device, browser = parse_device(ua)
    ip_hash = hash_ip(ip)
    suspicious = False
    if ip_hash:
        recent = db.scalar(select(func.count(Click.id)).where(
            Click.affiliate_id == affiliate.id, Click.ip_hash == ip_hash,
            Click.created_at >= utcnow() - timedelta(hours=1))) or 0
        suspicious = recent >= 50
    click = Click(referral_link_id=link.id if link else None, affiliate_id=affiliate.id, product_id=product_id,
                  tracking_id=secrets.token_hex(12), landing_page=landing[:800],
                  referrer=(referrer or "")[:500] or None, device_type=device, browser=browser,
                  country=country, ip_hash=ip_hash, suspicious=suspicious)
    db.add(click)
    db.flush()
    return click


def resolve_attribution(db: Session, org: Organization, tracking_id: str | None, coupon_code: str | None, at):
    """Last eligible affiliate click wins; coupon is the fallback. Returns (affiliate, click, type) or None."""
    if tracking_id:
        row = db.execute(select(Click, Affiliate).join(Affiliate, Affiliate.id == Click.affiliate_id).where(
            Click.tracking_id == tracking_id, Affiliate.organization_id == org.id)).first()
        if row:
            click, aff = row
            within = at - click.created_at <= timedelta(days=org.attribution_days)
            if within and aff.status == "APPROVED":
                return aff, click, "LAST_CLICK"
    if coupon_code:
        row = db.execute(select(Coupon, Affiliate).join(Affiliate, Affiliate.id == Coupon.affiliate_id).where(
            Coupon.organization_id == org.id, Coupon.code == coupon_code.strip().upper(),
            Coupon.active.is_(True))).first()
        if row and row[1].status == "APPROVED":
            return row[1], None, "COUPON"
    return None
