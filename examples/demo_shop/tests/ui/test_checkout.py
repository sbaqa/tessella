"""The three-step checkout: details, overview with discount, thank-you page."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import LAPIS, MARBLE, TERRACOTTA, Delivery, discounted, money
from tests.framework.pages.screens import CheckoutInfoPage, OrdersPage

pytestmark = pytest.mark.ui


def _to_details(shopper, cart, *products) -> CheckoutInfoPage:
    shopper.sign_in()
    shopper.fill_cart(*products)
    cart.header.open_cart()
    return cart.checkout()


@pytest.mark.parametrize("blank,message", [("first_name", "First name is required"),
                                           ("last_name", "Last name is required"),
                                           ("postal_code", "Postal code is required")])
def test_details_are_required(shopper, cart, blank, message):
    info = _to_details(shopper, cart, LAPIS)
    info.fill(Delivery(**{blank: ""}))
    info.submit()
    info.expect_error(message)


def test_overview_shows_the_item_total(shopper, cart):
    overview = _to_details(shopper, cart, LAPIS, TERRACOTTA).continue_to_overview(Delivery())
    expect(overview.item_total).to_have_text(money(LAPIS.price + TERRACOTTA.price))
    expect(overview.total).to_have_text(money(LAPIS.price + TERRACOTTA.price))
    expect(overview.page.get_by_text("Anna Tiler, 10115")).to_be_visible()


def test_discount_lowers_the_total(shopper, cart):
    overview = _to_details(shopper, cart, MARBLE).continue_to_overview(Delivery())
    overview.apply_discount("tile25")
    expect(overview.total).to_have_text(money(discounted(MARBLE.price, "TILE25")))


def test_unknown_discount_is_explained(shopper, cart):
    overview = _to_details(shopper, cart, MARBLE).continue_to_overview(Delivery())
    overview.apply_discount("FREESTUFF")
    expect(overview.page.locator("#code-error")).to_have_text("FREESTUFF is not a discount code we know")
    expect(overview.total).to_have_text(money(MARBLE.price))


def test_cancel_on_overview_returns_to_the_cart(shopper, cart):
    overview = _to_details(shopper, cart, LAPIS).continue_to_overview(Delivery())
    back = overview.cancel()
    expect(back.line(LAPIS)).to_be_visible()


def test_placing_an_order_thanks_you_and_empties_the_cart(shopper):
    shopper.sign_in()
    order_id = shopper.buy(LAPIS, TERRACOTTA)
    expect(shopper.page.get_by_role("heading", name="Thank you for your order!")).to_be_visible()
    assert order_id >= 1001
    shopper.page.get_by_test_id("cart-count").wait_for()
    expect(shopper.page.get_by_test_id("cart-count")).to_have_text("0")


def test_placed_order_shows_as_paid_in_my_orders(shopper, page):
    shopper.sign_in()
    order_id = shopper.buy(LAPIS, code="MOSAIC10")
    orders = OrdersPage(page)
    orders.menu.open().go("orders")
    expect(orders.status(order_id)).to_have_text("paid")
    expect(page.get_by_test_id(f"order-{order_id}")).to_contain_text("MOSAIC10")


def test_checkout_without_a_cart_goes_back_to_the_cart(shopper):
    shopper.sign_in()
    CheckoutInfoPage(shopper.page).open()
    expect(shopper.page.get_by_role("heading", name="Your cart")).to_be_visible()
