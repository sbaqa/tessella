"""My orders: paying and cancelling from the browser.

test_cancel_discounted_order fails while planted bug 1 is active: cancelling a paid,
discounted order shows a red error toast (the server answers 500).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import LAPIS, MARBLE, money

pytestmark = pytest.mark.ui


def test_cancel_discounted_order(shopper, orders_page):
    """Fails while planted bug 1 is active: the cancel request returns 500."""
    shopper.sign_in()
    order_id = shopper.buy(MARBLE, MARBLE, code="TILE25")
    orders_page.open()
    expect(orders_page.status(order_id)).to_have_text("paid")
    orders_page.cancel(order_id)
    orders_page.toast.expect_text(f"Order {order_id} cancelled")


def test_cancel_paid_order_shows_the_refund(shopper, orders_page):
    shopper.sign_in()
    order_id = shopper.buy(LAPIS)
    orders_page.open()
    orders_page.cancel(order_id)
    orders_page.toast.expect_text(f"Order {order_id} cancelled, {money(LAPIS.price)} refunded")
    expect(orders_page.status(order_id)).to_have_text("cancelled")


def test_an_unpaid_order_can_be_paid_from_my_orders(shopper, orders_page, orders):
    shopper.sign_in("anna")
    created = orders.create([{"product_id": LAPIS.id, "qty": 1}]).json()  # placed elsewhere, unpaid
    orders_page.open()
    expect(orders_page.status(created["id"])).to_have_text("created")
    orders_page.pay(created["id"])
    expect(orders_page.status(created["id"])).to_have_text("paid")


def test_no_orders_yet(shopper, orders_page):
    shopper.sign_in("bob")
    orders_page.open()
    expect(orders_page.empty).to_be_visible()
