import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

from app.core.security import sign_payload
from app.database import SessionLocal
from app.models import Click
from app.utils import money, utcnow
from tests.conftest import H, click, order


def test_auth_and_rbac(client, biz):
    assert client.post("/api/auth/login", json={"email": "owner@acme.com", "password": "wrong-pass"}).status_code == 401
    assert client.get("/api/affiliates").status_code == 401
    assert client.get("/api/affiliates", headers=H(biz["atok"])).status_code == 403
    assert client.get("/api/affiliates", headers=H(biz["tok"])).status_code == 200
    # cross-organization isolation
    other = client.post("/api/auth/register", json={"org_name": "Other Co", "name": "O", "email": "o@other.com",
                                                    "password": "password123"}).json()["access_token"]
    assert client.get(f"/api/affiliates/{biz['aff']['id']}", headers=H(other)).status_code == 404
    assert client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(other)).status_code == 404
    assert client.get("/api/affiliates", headers=H(other)).json() == []


def test_password_reset(client, biz):
    tok = client.post("/api/auth/reset-password", json={"email": "owner@acme.com"}).json()["token"]
    assert client.post("/api/auth/reset-password/confirm", json={"token": tok, "new_password": "newpassword1"}).status_code == 200
    assert client.post("/api/auth/reset-password/confirm", json={"token": tok, "new_password": "another-pass1"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "owner@acme.com", "password": "newpassword1"}).status_code == 200


def test_unapproved_affiliate_cannot_link(client, biz):
    r = client.post("/api/auth/register-affiliate", json={"org_slug": biz["org"]["slug"], "name": "New", "email": "n@x.com",
                                                          "password": "password123", "accept_terms": True})
    assert client.post("/api/referral-links", headers=H(r.json()["access_token"]), json={"product_id": biz["prod"]["id"]}).status_code == 403
    assert client.post("/api/auth/register-affiliate", json={"org_slug": biz["org"]["slug"], "name": "N", "email": "z@x.com",
                                                             "password": "password123", "accept_terms": False}).status_code == 422


def test_full_lifecycle(client, biz):
    tid = click(client, biz["link"])
    r = order(client, biz, "ORD-1", tid)
    assert r.status_code == 200, r.text
    conv = r.json()["conversion"]
    assert r.json()["order"]["eligible_amount"] == "4500.00" and conv["flag_status"] == "NONE"
    # idempotent duplicate
    d = order(client, biz, "ORD-1", tid).json()
    assert d["duplicate"] is True
    ledger = client.get("/api/commissions", headers=H(biz["atok"])).json()
    assert len(ledger) == 1 and ledger[0]["amount"] == "900.00"
    bal = client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(biz["atok"])).json()
    assert bal["payable"] == "900.00" and bal["total_earned"] == "900.00"
    # partial refund of 1000 on 4500 eligible -> 20% of 1000 = 200 reversal
    rf = client.post("/api/refunds", headers=H(biz["tok"]), json={"external_order_id": "ORD-1", "amount": "1000", "reason": "partial"})
    assert rf.status_code == 201 and rf.json()["order_status"] == "PARTIALLY_REFUNDED"
    bal = client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(biz["atok"])).json()
    assert bal["payable"] == "700.00" and bal["reversed"] == "200.00"
    # over-refund rejected
    assert client.post("/api/refunds", headers=H(biz["tok"]), json={"external_order_id": "ORD-1", "amount": "9999"}).status_code == 422
    # payout
    batch = client.post("/api/payouts/generate", headers=H(biz["tok"])).json()
    assert batch["total_amount"] == "700.00" and batch["affiliate_count"] == 1
    assert client.post("/api/payouts/generate", headers=H(biz["tok"])).status_code == 409  # funds already reserved
    assert client.post(f"/api/payouts/batches/{batch['id']}/process", headers=H(biz["tok"])).status_code == 409  # not approved
    assert client.post(f"/api/payouts/batches/{batch['id']}/approve", headers=H(biz["tok"])).json()["status"] == "APPROVED"
    assert client.post(f"/api/payouts/batches/{batch['id']}/process", headers=H(biz["tok"])).json()["status"] == "PROCESSING"
    assert client.post(f"/api/payouts/batches/{batch['id']}/process", headers=H(biz["tok"])).status_code == 409
    pay = client.get(f"/api/payouts/batches/{batch['id']}", headers=H(biz["tok"])).json()["payouts"][0]
    assert pay["status"] == "PROCESSING" and pay["provider_reference"]
    # webhook: signature, success, idempotency
    body = json.dumps({"event_id": "evt_1", "type": "payout.completed", "reference": pay["provider_reference"]}).encode()
    assert client.post("/api/webhooks/payment", content=body, headers={"X-Signature": "bad"}).status_code == 401
    sig = {"X-Signature": sign_payload(body, "test-secret")}
    assert client.post("/api/webhooks/payment", content=body, headers=sig).json()["result"] == "paid"
    assert client.post("/api/webhooks/payment", content=body, headers=sig).json()["result"] == "duplicate"
    bal = client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(biz["atok"])).json()
    assert bal["paid"] == "700.00" and bal["payable"] == "0.00"
    assert client.get(f"/api/payouts/batches/{batch['id']}", headers=H(biz["tok"])).json()["status"] == "PAID"
    notes = client.get("/api/auth/notifications", headers=H(biz["atok"])).json()
    assert any(n["kind"] == "payout.completed" for n in notes)
    dash = client.get("/api/analytics/affiliate", headers=H(biz["atok"])).json()
    assert dash["clicks"] == 1 and dash["conversions"] == 1 and dash["conversion_rate"] == "100.00"
    biz_dash = client.get("/api/analytics/business", headers=H(biz["tok"])).json()
    assert biz_dash["completed_payouts"] == "700.00" and biz_dash["total_affiliates"] == 1
    assert len(client.get("/api/audit-logs", headers=H(biz["tok"])).json()) > 5


def test_failed_payout_returns_funds_and_retry(client, biz):
    order(client, biz, "ORD-F", click(client, biz["link"]))
    b = client.post("/api/payouts/generate", headers=H(biz["tok"])).json()
    client.post(f"/api/payouts/batches/{b['id']}/approve", headers=H(biz["tok"]))
    client.post(f"/api/payouts/batches/{b['id']}/process", headers=H(biz["tok"]))
    pid = client.get("/api/payouts", headers=H(biz["tok"])).json()[0]["id"]
    assert client.post(f"/api/payouts/{pid}/simulate", headers=H(biz["tok"]), json={"outcome": "failed"}).json()["result"] == "failed"
    aid = biz["aff"]["id"]
    assert client.get(f"/api/affiliates/{aid}/balance", headers=H(biz["tok"])).json()["payable"] == "900.00"
    assert client.get(f"/api/payouts/batches/{b['id']}", headers=H(biz["tok"])).json()["status"] == "PARTIALLY_FAILED"
    assert client.post(f"/api/payouts/{pid}/retry", headers=H(biz["tok"])).json()["status"] == "PROCESSING"
    client.post(f"/api/payouts/{pid}/simulate", headers=H(biz["tok"]), json={"outcome": "completed"})
    assert client.get(f"/api/affiliates/{aid}/balance", headers=H(biz["tok"])).json()["paid"] == "900.00"


def test_holding_period_and_min_threshold(client, biz):
    client.patch("/api/organization", headers=H(biz["tok"]), json={"hold_days": 30})
    order(client, biz, "ORD-H", click(client, biz["link"]))
    bal = client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(biz["atok"])).json()
    assert bal["pending"] == "900.00" and bal["payable"] == "0.00"
    assert client.post("/api/payouts/generate", headers=H(biz["tok"])).status_code == 409
    client.patch("/api/organization", headers=H(biz["tok"]), json={"hold_days": 0, "min_payout": "5000"})
    order(client, biz, "ORD-H2", click(client, biz["link"]))  # payable 900 < 5000 minimum
    assert client.post("/api/payouts/generate", headers=H(biz["tok"])).status_code == 409


def test_attribution_window(client, biz):
    tid = click(client, biz["link"])
    with SessionLocal() as db:
        c = db.query(Click).filter_by(tracking_id=tid).one()
        c.created_at = utcnow() - timedelta(days=45)
        db.commit()
    assert order(client, biz, "ORD-OLD", tid).json()["conversion"] is None
    assert order(client, biz, "ORD-NONE", None).json()["conversion"] is None


def test_self_referral_flag_and_review(client, biz):
    r = order(client, biz, "ORD-S", click(client, biz["link"]), customer="rahul@x.com").json()
    assert r["conversion"]["flag_status"] == "FLAGGED"
    aid = biz["aff"]["id"]
    bal = client.get(f"/api/affiliates/{aid}/balance", headers=H(biz["tok"])).json()
    assert bal["payable"] == "0.00" and bal["on_hold"] == "900.00"
    assert client.post("/api/payouts/generate", headers=H(biz["tok"])).status_code == 409
    cid = r["conversion"]["id"]
    assert client.post(f"/api/conversions/{cid}/review", headers=H(biz["tok"]), json={"decision": "reject"}).status_code == 200
    bal = client.get(f"/api/affiliates/{aid}/balance", headers=H(biz["tok"])).json()
    assert bal["payable"] == "0.00" and bal["on_hold"] == "0.00" and bal["ledger_balance"] == "0.00"


def test_chargeback_and_coupon(client, biz):
    t = biz["tok"]
    assert client.post("/api/coupons", headers=H(t), json={"affiliate_id": biz["aff"]["id"], "code": "rahul20"}).status_code == 201
    r = order(client, biz, "ORD-C", None, coupon_code="RAHUL20").json()
    assert r["conversion"]["attribution_type"] == "COUPON"
    cb = client.post("/api/refunds", headers=H(t), json={"external_order_id": "ORD-C", "kind": "CHARGEBACK"})
    assert cb.json()["order_status"] == "CHARGEBACK"
    bal = client.get(f"/api/affiliates/{biz['aff']['id']}/balance", headers=H(t)).json()
    assert bal["ledger_balance"] == "0.00" and bal["reversed"] == "900.00"
    assert client.post("/api/refunds", headers=H(t), json={"external_order_id": "ORD-C"}).status_code == 409


def test_tier_commission(client, biz):
    t = biz["tok"]
    client.patch("/api/organization", headers=H(t), json={"commission_mode": "SALES_TIER"})
    client.post("/api/commission-tiers", headers=H(t), json={"name": "Bronze", "min_value": "0", "max_value": "5000", "commission_rate": "10"})
    client.post("/api/commission-tiers", headers=H(t), json={"name": "Silver", "min_value": "5000.01", "commission_rate": "15"})
    order(client, biz, "T1", click(client, biz["link"]), amount="4000", discount="0")  # month total 4000 -> 10% = 400
    order(client, biz, "T2", click(client, biz["link"]), amount="4000", discount="0")  # month total 8000 -> 15% = 600
    amounts = sorted(e["amount"] for e in client.get("/api/commissions", headers=H(t)).json())
    assert amounts == ["400.00", "600.00"]


def test_invalid_inputs(client, biz):
    h = {"X-Api-Key": biz["api_key"]}
    assert client.post("/api/webhooks/orders", json={}, headers={"X-Api-Key": "nope"}).status_code in (401, 422)
    assert client.post("/api/webhooks/orders", headers=h, json={"external_order_id": "X", "customer_reference": "a", "product_id": 999, "amount": "10"}).status_code == 404
    assert client.post("/api/webhooks/orders", headers=h, json={"external_order_id": "X", "customer_reference": "a", "product_id": biz["prod"]["id"], "amount": "10", "discount": "20"}).status_code == 422
    assert client.get("/r/NOPE").status_code == 404
    # affiliate cannot read staff data or another affiliate's balance
    assert client.get("/api/payouts/batches", headers=H(biz["atok"])).status_code == 403
    assert client.post("/api/payouts/generate", headers=H(biz["atok"])).status_code == 403


def test_money_rounding():
    assert money("0.005") == Decimal("0.01") and money(Decimal("1234.5")) == Decimal("1234.50")
