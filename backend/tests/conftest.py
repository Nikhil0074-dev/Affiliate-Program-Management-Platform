import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["EXPOSE_RESET_TOKEN"] = "true"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def H(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def biz(client):
    """Business with hold_days=0, one product at 20%, one approved affiliate with a link."""
    r = client.post("/api/auth/register", json={"org_name": "Acme Courses", "name": "Owner", "email": "owner@acme.com",
                                                "password": "password123"})
    assert r.status_code == 201, r.text
    tok, org = r.json()["access_token"], r.json()["organization"]
    org_full = client.get("/api/organization", headers=H(tok)).json()
    assert client.patch("/api/organization", headers=H(tok), json={"hold_days": 0, "min_payout": "100"}).status_code == 200
    prod = client.post("/api/products", headers=H(tok), json={"name": "Python Masterclass", "price": "5000",
                                                              "base_url": "https://example.com/python"}).json()
    client.post("/api/commission-rules", headers=H(tok), json={"product_id": prod["id"], "type": "PERCENT", "rate": "20"})
    r = client.post("/api/auth/register-affiliate", json={"org_slug": org["slug"], "name": "Rahul", "email": "rahul@x.com",
                                                          "password": "password123", "accept_terms": True})
    assert r.status_code == 201, r.text
    atok, aff = r.json()["access_token"], r.json()["affiliate"]
    client.post(f"/api/affiliates/{aff['id']}/approve", headers=H(tok))
    link = client.post("/api/referral-links", headers=H(atok), json={"product_id": prod["id"]}).json()
    return dict(tok=tok, atok=atok, org=org, api_key=org_full["api_key"], prod=prod, aff=aff, link=link)


def click(client, link):
    r = client.get(f"/r/{link['code']}", follow_redirects=False, headers={"user-agent": "Mozilla/5.0 Chrome"})
    assert r.status_code == 302
    return r.cookies.get("aff_tid") or r.headers["location"].split("tid=")[1]


def order(client, b, oid, tid=None, amount="5000", discount="500", customer="buyer@y.com", **kw):
    return client.post("/api/webhooks/orders", headers={"X-Api-Key": b["api_key"]}, json={
        "external_order_id": oid, "customer_reference": customer, "product_id": b["prod"]["id"],
        "amount": amount, "discount": discount, "tracking_id": tid, **kw})
