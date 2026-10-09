from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.security import hash_password
from ..database import get_db
from ..deps import current_user, my_affiliate, org_of, staff_user
from ..models import Affiliate, Organization, User
from ..services.audit import audit, notify
from ..services.commission import balances
from ..utils import AppError, serialize, utcnow
from .auth import _new_affiliate_code

router = APIRouter(prefix="/api/affiliates", tags=["affiliates"])


class AffiliateCreate(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=8)
    website: str | None = None
    marketing_channels: str | None = None
    country: str | None = None


class AffiliatePatch(BaseModel):
    website: str | None = None
    marketing_channels: str | None = None
    country: str | None = None
    review_notes: str | None = None


class Review(BaseModel):
    notes: str | None = None


def _out(a: Affiliate, u: User) -> dict:
    return serialize(a, name=u.name, email=u.email)


def _load(db: Session, org: Organization, aff_id: int) -> tuple[Affiliate, User]:
    a = db.scalar(select(Affiliate).where(Affiliate.id == aff_id, Affiliate.organization_id == org.id))
    if not a:
        raise AppError(404, "Affiliate not found")
    return a, db.get(User, a.user_id)


@router.get("")
def list_affiliates(status: str | None = None, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                    db: Session = Depends(get_db)):
    q = select(Affiliate, User).join(User, User.id == Affiliate.user_id).where(Affiliate.organization_id == org.id)
    if status:
        q = q.where(Affiliate.status == status.upper())
    return [_out(a, u) for a, u in db.execute(q.order_by(Affiliate.id.desc())).all()]


@router.post("", status_code=201)
def create_affiliate(body: AffiliateCreate, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                     db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise AppError(409, "Email already registered")
    u = User(organization_id=org.id, name=body.name, email=email, role="AFFILIATE",
             password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    a = Affiliate(organization_id=org.id, user_id=u.id, code=_new_affiliate_code(db), status="APPROVED",
                  website=body.website, marketing_channels=body.marketing_channels, country=body.country,
                  joined_at=utcnow(), terms_version=org.terms_version)
    db.add(a)
    db.flush()
    audit(db, org.id, user.id, "affiliate.created", "affiliate", a.id)
    db.commit()
    return _out(a, u)


@router.get("/me")
def my_profile(aff: Affiliate = Depends(my_affiliate), db: Session = Depends(get_db)):
    return {**_out(aff, db.get(User, aff.user_id)), "balance": balances(db, aff.id)}


@router.get("/{aff_id}")
def get_affiliate(aff_id: int, user: User = Depends(staff_user), org: Organization = Depends(org_of),
                  db: Session = Depends(get_db)):
    a, u = _load(db, org, aff_id)
    return _out(a, u)


@router.patch("/{aff_id}")
def patch_affiliate(aff_id: int, body: AffiliatePatch, user: User = Depends(staff_user),
                    org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    a, u = _load(db, org, aff_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    audit(db, org.id, user.id, "affiliate.updated", "affiliate", a.id)
    db.commit()
    return _out(a, u)


def _transition(aff_id, allowed, new_status, action, user, org, db, notes=None, message=None):
    a, u = _load(db, org, aff_id)
    if a.status not in allowed:
        raise AppError(409, f"Cannot move affiliate from {a.status} to {new_status}")
    a.status = new_status
    if notes:
        a.review_notes = notes
    if new_status == "APPROVED" and not a.joined_at:
        a.joined_at = utcnow()
    if message:
        notify(db, u.id, f"affiliate.{new_status.lower()}", message)
    audit(db, org.id, user.id, action, "affiliate", a.id, notes=notes)
    db.commit()
    return _out(a, u)


@router.post("/{aff_id}/review")
def start_review(aff_id: int, body: Review = Review(), user: User = Depends(staff_user),
                 org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _transition(aff_id, {"PENDING"}, "UNDER_REVIEW", "affiliate.review_started", user, org, db, body.notes)


@router.post("/{aff_id}/approve")
def approve(aff_id: int, body: Review = Review(), user: User = Depends(staff_user),
            org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _transition(aff_id, {"PENDING", "UNDER_REVIEW"}, "APPROVED", "affiliate.approved", user, org, db,
                       body.notes, "Your affiliate application has been approved.")


@router.post("/{aff_id}/reject")
def reject(aff_id: int, body: Review = Review(), user: User = Depends(staff_user),
           org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _transition(aff_id, {"PENDING", "UNDER_REVIEW"}, "REJECTED", "affiliate.rejected", user, org, db,
                       body.notes, "Your affiliate application was not approved.")


@router.post("/{aff_id}/suspend")
def suspend(aff_id: int, body: Review = Review(), user: User = Depends(staff_user),
            org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _transition(aff_id, {"APPROVED"}, "SUSPENDED", "affiliate.suspended", user, org, db, body.notes)


@router.post("/{aff_id}/reactivate")
def reactivate(aff_id: int, body: Review = Review(), user: User = Depends(staff_user),
               org: Organization = Depends(org_of), db: Session = Depends(get_db)):
    return _transition(aff_id, {"SUSPENDED"}, "APPROVED", "affiliate.reactivated", user, org, db, body.notes)


@router.get("/{aff_id}/balance")
def balance(aff_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "AFFILIATE":
        a = db.scalar(select(Affiliate).where(Affiliate.user_id == user.id))
        if not a or a.id != aff_id:
            raise AppError(403, "You can only view your own balance")
    elif user.role in ("OWNER", "FINANCE"):
        a = db.scalar(select(Affiliate).where(Affiliate.id == aff_id, Affiliate.organization_id == user.organization_id))
        if not a:
            raise AppError(404, "Affiliate not found")
    else:
        raise AppError(403, "Access denied")
    return balances(db, a.id)
