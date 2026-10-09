import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core.security import decode_token
from .database import get_db
from .models import Affiliate, Organization, User
from .utils import AppError

bearer = HTTPBearer(auto_error=False)
STAFF_ROLES = {"OWNER", "FINANCE"}


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not creds:
        raise AppError(401, "Not authenticated")
    try:
        payload = decode_token(creds.credentials)
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(401, "Invalid or expired token")
    if not user or user.status != "ACTIVE":
        raise AppError(401, "Invalid or expired token")
    return user


def staff_user(user: User = Depends(current_user)) -> User:
    if user.role not in STAFF_ROLES:
        raise AppError(403, "Staff access required")
    return user


def owner_user(user: User = Depends(current_user)) -> User:
    if user.role != "OWNER":
        raise AppError(403, "Owner access required")
    return user


def platform_admin(user: User = Depends(current_user)) -> User:
    if user.role != "PLATFORM_ADMIN":
        raise AppError(403, "Platform administrator access required")
    return user


def org_of(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Organization:
    org = db.get(Organization, user.organization_id) if user.organization_id else None
    if not org:
        raise AppError(403, "No organization associated with this user")
    return org


def my_affiliate(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Affiliate:
    if user.role != "AFFILIATE":
        raise AppError(403, "Affiliate access required")
    aff = db.scalar(select(Affiliate).where(Affiliate.user_id == user.id))
    if not aff:
        raise AppError(404, "Affiliate profile not found")
    return aff


def org_from_api_key(x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> Organization:
    org = db.scalar(select(Organization).where(Organization.api_key == (x_api_key or "")))
    if not org:
        raise AppError(401, "Invalid API key")
    return org
