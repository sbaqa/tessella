"""API tests for orders. One of these fails while planted bug 1 is active."""

from __future__ import annotations


def place(api, auth, items=None, code=None) -> dict:
    res = api.post("/api/orders", headers=auth,
                   json={"items": items or [{"product_id": 1, "qty": 2}], "discount_code": code})
    assert res.status_code == 201, res.text
    return res.json()


def test_create_order_requires_login(api):
    res = api.post("/api/orders", json={"items": [{"product_id": 1, "qty": 1}]})
    assert res.status_code == 401


def test_create_and_pay_order(api, anna):
    order = place(api, anna)
    assert order["status"] == "created"
    assert order["total"] == 2 * 1450

    paid = api.post(f"/api/orders/{order['id']}/pay", headers=anna)
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"


def test_discount_is_applied(api, anna):
    order = place(api, anna, items=[{"product_id": 4, "qty": 2}], code="tile25")
    assert order["discount_code"] == "TILE25"
    assert order["total"] == round(2 * 2490 * 0.75)


def test_unknown_discount_code_is_rejected(api, anna):
    res = api.post("/api/orders", headers=anna,
                   json={"items": [{"product_id": 1, "qty": 1}], "discount_code": "FREESTUFF"})
    assert res.status_code == 400
    assert "unknown discount code" in res.json()["error"]


def test_cancel_unpaid_order(api, anna):
    order = place(api, anna)
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"
    assert res.json()["refunded"] == 0


def test_cancel_paid_order_refunds_in_full(api, anna):
    order = place(api, anna)
    api.post(f"/api/orders/{order['id']}/pay", headers=anna).raise_for_status()
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 200
    assert res.json()["refunded"] == order["total"]


def test_cancel_paid_discounted_order_refunds_in_full(api, anna):
    """Fails while bug 1 is active: the refund lookup crashes with a 500."""
    order = place(api, anna, code="MOSAIC10")
    api.post(f"/api/orders/{order['id']}/pay", headers=anna).raise_for_status()
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 200, f"cancel returned {res.status_code}: {res.text}"
    assert res.json()["refunded"] == order["total"]


def test_shipped_order_cannot_be_cancelled(api, anna):
    order = place(api, anna)
    api.post(f"/api/orders/{order['id']}/pay", headers=anna).raise_for_status()
    api.post(f"/api/orders/{order['id']}/ship", headers=anna).raise_for_status()
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 409
    # Note: this test does not check the order afterwards, which is why bug 5 slips through.
