"""Demo data:  python -m app.seed   |   Platform admin:  python -m app.seed --admin you@x.com 'password'"""
import secrets
import sys

from sqlalchemy import select

from .core.security import hash_password
from .database import Base, SessionLocal, engine
from .models import Affiliate, CommissionRule, Organization, Product, User
from .utils import utcnow


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if len(sys.argv) >= 4 and sys.argv[1] == "--admin":
            db.add(User(name="Platform Admin", email=sys.argv[2].lower(), role="PLATFORM_ADMIN",
                        password_hash=hash_password(sys.argv[3])))
            db.commit()
            print("Platform admin created")
            return
        if db.scalar(select(Organization).where(Organization.slug == "demo-academy")):
            print("Demo data already exists")
            return
        org = Organization(name="Demo Academy", slug="demo-academy", currency="INR", hold_days=0,
                           min_payout=100, api_key="ak_" + secrets.token_urlsafe(24))
        db.add(org)
        db.flush()
        db.add(User(organization_id=org.id, name="Demo Owner", email="owner@demo-academy.com", role="OWNER",
                    password_hash=hash_password("demo12345")))
        au = User(organization_id=org.id, name="Rahul Affiliate", email="rahul@demo-academy.com", role="AFFILIATE",
                  password_hash=hash_password("demo12345"))
        db.add(au)
        db.flush()
        db.add(Affiliate(organization_id=org.id, user_id=au.id, code="AFF1024", status="APPROVED", joined_at=utcnow(),
                         terms_version="v1", terms_accepted_at=utcnow()))
        p = Product(organization_id=org.id, name="Python Masterclass", price=5000, currency="INR",
                    base_url="https://example.com/python-masterclass")
        db.add(p)
        db.flush()
        db.add(CommissionRule(organization_id=org.id, product_id=p.id, type="PERCENT", rate=20))
        db.commit()
        print(f"Seeded. Owner: owner@demo-academy.com / demo12345   Affiliate: rahul@demo-academy.com / demo12345\nAPI key: {org.api_key}")


if __name__ == "__main__":
    main()
