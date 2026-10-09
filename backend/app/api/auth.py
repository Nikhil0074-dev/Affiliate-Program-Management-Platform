import logging
import secrets
import string

import jwt
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.security import create_token, decode_token, hash_password, verify_password
from ..database import get_db
from ..deps import current_user
from ..models import Affiliate, Notification, Organization, User
from ..services.audit import audit, notify_staff
from ..utils import AppError, serialize, slugify, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("affiliate")


class BusinessRegister(BaseModel):
    org_name: str = Field(min_length=2, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    currency: str = Field(default="INR", min_length=3, max_length=3)


class AffiliateRegister(BaseModel):
    org_slug: str
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    website: str | None = None
    marketing_channels: str | None = None
    country: str | None = None
    accept_terms: bool = False


class Login(BaseModel):
    email: EmailStr
    password: str


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


def _user_out(u: User) -> dict:
    return serialize(u, exclude=("password_hash",))


def _token_response(u: User) -> dict:
    return {"access_token": create_token(u.id), "token_type": "bearer", "user": _user_out(u)}


def _new_affiliate_code(db: Session) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "AFF" + "".join(secrets.choice(alphabet) for _ in range(5))
        if not db.scalar(select(Affiliate.id).where(Affiliate.code == code)):
            return code


def _email_taken(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email.lower())) is not None


@router.post("/register", status_code=201)
def register_business(body: BusinessRegister, db: Session = Depends(get_db)):
    email = body.email.lower()
    if _email_taken(db, email):
        raise AppError(409, "Email already registered")
    slug, base = slugify(body.org_name), slugify(body.org_name)
    while db.scalar(select(Organization.id).where(Organization.slug == slug)):
        slug = f"{base}-{secrets.token_hex(2)}"
    org = Organization(name=body.org_name, slug=slug, currency=body.currency.upper(),
                       api_key="ak_" + secrets.token_urlsafe(24))
    db.add(org)
    db.flush()
    user = User(organization_id=org.id, name=body.name, email=email, role="OWNER",
                password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    audit(db, org.id, user.id, "organization.created", "organization", org.id)
    db.commit()
    return {**_token_response(user), "organization": {"id": org.id, "slug": org.slug, "name": org.name}}


@router.post("/register-affiliate", status_code=201)
def register_affiliate(body: AffiliateRegister, db: Session = Depends(get_db)):
    org = db.scalar(select(Organization).where(Organization.slug == body.org_slug))
    if not org:
        raise AppError(404, "Affiliate program not found")
    if not body.accept_terms:
        raise AppError(422, "You must accept the affiliate terms")
    email = body.email.lower()
    if _email_taken(db, email):
        raise AppError(409, "Email already registered")
    user = User(organization_id=org.id, name=body.name, email=email, role="AFFILIATE",
                password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    aff = Affiliate(organization_id=org.id, user_id=user.id, code=_new_affiliate_code(db), status="PENDING",
                    website=body.website, marketing_channels=body.marketing_channels, country=body.country,
                    terms_version=org.terms_version, terms_accepted_at=utcnow())
    db.add(aff)
    db.flush()
    notify_staff(db, org.id, "affiliate.applied", f"New affiliate application from {body.name}.")
    audit(db, org.id, user.id, "affiliate.applied", "affiliate", aff.id)
    db.commit()
    return {**_token_response(user), "affiliate": serialize(aff)}


@router.post("/login")
def login(body: Login, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or user.status != "ACTIVE" or not verify_password(body.password, user.password_hash):
        raise AppError(401, "Invalid email or password")
    return _token_response(user)


@router.post("/logout")
def logout(_: User = Depends(current_user)):
    # Stateless JWT: the client discards the token.
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    aff = db.scalar(select(Affiliate).where(Affiliate.user_id == user.id))
    return {"user": _user_out(user), "affiliate": serialize(aff) if aff else None}


@router.post("/reset-password")
def reset_password(body: ResetRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    resp = {"message": "If the account exists, a reset link has been sent."}
    if user:
        token = create_token(user.id, purpose="reset", minutes=30, fp=user.password_hash[-12:])
        log.info("PASSWORD RESET EMAIL to=%s", user.email)
        if settings.expose_reset_token:
            resp["token"] = token
    return resp


@router.post("/reset-password/confirm")
def reset_confirm(body: ResetConfirm, db: Session = Depends(get_db)):
    try:
        payload = decode_token(body.token, purpose="reset")
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(400, "Invalid or expired reset token")
    if not user or payload.get("fp") != user.password_hash[-12:]:
        raise AppError(400, "Invalid or expired reset token")  # token is single-use: hash changes after reset
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}


@router.get("/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)
                      .order_by(Notification.id.desc()).limit(50)).all()
    return [serialize(n) for n in rows]
