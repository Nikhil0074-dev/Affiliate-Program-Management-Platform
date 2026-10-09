"""Month-end payout generation. Run from cron / a scheduler:  python -m app.workers.monthly"""
import logging

from sqlalchemy import select

from ..database import Base, SessionLocal, engine
from ..models import Organization
from ..services.payout import generate_batch
from ..utils import AppError

log = logging.getLogger("affiliate.worker")


def run_monthly_payouts() -> list[int]:
    Base.metadata.create_all(bind=engine)
    created = []
    with SessionLocal() as db:
        for org in db.scalars(select(Organization)).all():
            try:
                created.append(generate_batch(db, org).id)
            except AppError as e:
                log.info("org %s skipped: %s", org.id, e.detail)
    return created


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Created payout batches:", run_monthly_payouts())
