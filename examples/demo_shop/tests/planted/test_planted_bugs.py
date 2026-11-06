"""Proof tests for the planted product bugs.

These are NOT part of the normal suite (pytest.ini deselects the `planted` marker). They
exist so you can check that each bug is really there, and really gone once fixed:

    pytest -m planted                          # all four fail while the bugs are active
    DEMO_SHOP_FIXED=all pytest -m planted      # all four pass

Bugs 3, 4 and 5 are the ones a normal suite misses. They are what Tessella's probe step
is for: identity swaps, parallel replays and state checks after a rejected request.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.helpers import login

pytestmark = pytest.mark.planted


def _paid_order(api, auth, code=None) -> dict:
    order = api.post("/api/orders", headers=auth,
                     json={"items": [{"product_id": 2, "qty": 3}], "discount_code": code}).json()
    api.post(f"/api/orders/{order['id']}/pay", headers=auth).raise_for_status()
    return order


def test_bug1_cancel_discounted_order(api, anna):
    order = _paid_order(api, anna, code="TILE25")
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 200, f"bug 1 active: cancel returned {res.status_code}"


def test_bug3_other_user_cannot_read_order(api, anna, bob):
    order = api.post("/api/orders", headers=anna,
                     json={"items": [{"product_id": 1, "qty": 1}]}).json()
    res = api.get(f"/api/orders/{order['id']}", headers=bob)
    assert res.status_code == 404, "bug 3 active: bob can read anna's order"


def test_bug4_parallel_pay_charges_once(shop_url, api, anna):
    order = api.post("/api/orders", headers=anna,
                     json={"items": [{"product_id": 6, "qty": 1}]}).json()

    def pay(_):
        with httpx.Client(base_url=shop_url, timeout=10) as c:
            return c.post(f"/api/orders/{order['id']}/pay", headers=anna).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(pay, range(4)))
    charges = api.get(f"/api/orders/{order['id']}/charges", headers=anna).json()
    assert len(charges) == 1, f"bug 4 active: {len(charges)} charges, responses {codes}"


def test_bug5_rejected_cancel_leaves_order_shipped(api, anna):
    order = _paid_order(api, anna)
    api.post(f"/api/orders/{order['id']}/ship", headers=anna).raise_for_status()
    rejected = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert rejected.status_code == 409
    after = api.get(f"/api/orders/{order['id']}", headers=anna).json()
    assert after["status"] == "shipped", f"bug 5 active: order is now {after['status']}"


def test_login_helper_still_works(api):
    """Sanity check for the helpers used above, so a failure here is not mistaken for a bug."""
    assert "Authorization" in login(api, "bob")
