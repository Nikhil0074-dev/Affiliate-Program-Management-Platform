# Affiliate Program Management Platform

Full-stack platform for digital-product affiliate programs: referral links, click tracking, last-click + coupon attribution,
configurable commission rules/tiers, append-only commission ledger, refunds/chargebacks, holding period, fraud flags,
monthly payout batches with approval workflow, signed payment webhooks, analytics, audit log and RBAC.

**Stack:** FastAPI + SQLAlchemy 2 + Pydantic (backend), PostgreSQL (SQLite for local/dev), JWT auth, a dependency-free
single-page frontend served by FastAPI (`frontend/index.html`), Docker, GitHub Actions, pytest.

## Run locally
```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.seed                                      # optional demo data
uvicorn app.main:app --reload                           # http://localhost:8000  (API docs: /docs)
python -m pytest -q                                     # run the test-suite
```
Demo logins after seeding: `owner@demo-academy.com` / `demo12345` (business) and `rahul@demo-academy.com` / `demo12345` (affiliate).
Affiliates sign up with the program slug (`demo-academy`).

## Docker (PostgreSQL)
```bash
docker compose up --build        # app on http://localhost:8000
```
Set `SECRET_KEY`, `PAYMENT_WEBHOOK_SECRET`, and `SIMULATE_PROVIDER=false` for production.

## Integrating your store
1. Send visitors through the affiliate link (`/r/{code}`): it records the click, sets an `aff_tid` cookie and redirects to the product URL with `ref`, campaign params and `tid`.
2. When an order is paid, call (API key from *Settings*):
```bash
curl -X POST http://localhost:8000/api/webhooks/orders -H "X-Api-Key: <key>" -H "Content-Type: application/json" \
 -d '{"external_order_id":"ORD-1","customer_reference":"buyer@x.com","product_id":1,"amount":"5000","discount":"500","tracking_id":"<tid>"}'
```
(`coupon_code` works without a tracking id.) Duplicate `external_order_id`s are ignored. Refunds: `POST /api/webhooks/orders/refund`.

## Key design points
- Money is `Decimal`/`NUMERIC`, currency stored explicitly; balances are always computed from the append-only `commission_ledger`.
- Payout generation reserves funds with a ledger entry, so commissions can never be paid twice; webhooks are signature-verified and idempotent.
- Object-level authorization: every query is scoped to the caller's organization; affiliates only see their own data.
- Fraud checks (self-referral, repeated customer, bursts, click flooding) *flag* conversions for review and hold their commission; they never auto-penalize.
- Raw IPs are never stored (salted hash only); no card/bank data is stored.
