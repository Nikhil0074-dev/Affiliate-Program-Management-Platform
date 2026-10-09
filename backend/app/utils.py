import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal


class AppError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


def utcnow() -> datetime:
    """Naive UTC timestamp (stored uniformly in UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mstr(value) -> str:
    return str(money(value))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "org"


def serialize(obj, exclude: tuple = (), **extra) -> dict:
    out = {}
    for attr in obj.__mapper__.column_attrs:  # attribute keys (e.g. AuditLog.meta, not the reserved "metadata")
        key = attr.key
        if key in exclude:
            continue
        v = getattr(obj, key)
        if isinstance(v, Decimal):
            v = str(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[key] = v
    out.update(extra)
    return out
