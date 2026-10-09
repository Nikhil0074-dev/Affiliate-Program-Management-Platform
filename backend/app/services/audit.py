import logging

from sqlalchemy.orm import Session

from ..models import AuditLog, Notification, User

log = logging.getLogger("affiliate")


def audit(db: Session, org_id, user_id, action: str, resource_type: str | None = None,
          resource_id=None, **meta) -> None:
    db.add(AuditLog(organization_id=org_id, user_id=user_id, action=action, resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id is not None else None, meta=meta or None))


def notify(db: Session, user_id: int, kind: str, message: str) -> None:
    """In-app notification + email hook (email delivery is logged; plug in SES/SMTP here)."""
    db.add(Notification(user_id=user_id, kind=kind, message=message))
    log.info("EMAIL user=%s kind=%s msg=%s", user_id, kind, message)


def notify_staff(db: Session, org_id: int, kind: str, message: str) -> None:
    from sqlalchemy import select
    for uid in db.scalars(select(User.id).where(User.organization_id == org_id, User.role.in_(["OWNER", "FINANCE"]))):
        notify(db, uid, kind, message)
