"""Browser tests with pytest-playwright. Two fail on purpose:

- test_login uses a stale selector (#login) from before the button was renamed to #sign-in.
  That is planted bug 2: a test bug, not a product bug.
- test_cancel_discounted_order hits planted bug 1 and shows a red error toast.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.helpers import USERS

pytestmark = pytest.mark.ui

expect.set_options(timeout=3000)


def open_shop(page: Page) -> None:
    page.goto("/")
    expect(page.locator("body[data-ready=true]")).to_be_attached()


def sign_in(page: Page, user: str = "anna") -> None:
    page.get_by_label("Username").fill(user)
    page.get_by_label("Password").fill(USERS[user])
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator("#who")).to_have_text(f"Signed in as {user}")


def place_order(page: Page, product_ids: list[int], code: str | None = None) -> int:
    for pid in product_ids:
        page.get_by_test_id(f"add-{pid}").click()
    if code:
        page.get_by_test_id("discount").fill(code)
    page.get_by_test_id("checkout").click()
    toast = page.get_by_test_id("toast")
    expect(toast).to_contain_text("placed")
    return int(re.search(r"Order (\d+)", toast.inner_text()).group(1))


def test_home_shows_all_patterns(page: Page):
    open_shop(page)
    expect(page.get_by_test_id(re.compile(r"^product-\d+$"))).to_have_count(6)
    expect(page.get_by_role("heading", name="Glazed tiles, sold by the sheet.")).to_be_visible()


def test_login(page: Page):
    """Planted bug 2 (test bug): the button id changed from #login to #sign-in."""
    open_shop(page)
    page.fill("#username", "anna")
    page.fill("#password", USERS["anna"])
    page.click("#login", timeout=3000)
    expect(page.locator("#who")).to_have_text("Signed in as anna")


def test_add_to_cart_updates_count(page: Page):
    open_shop(page)
    page.get_by_test_id("add-2").click()
    page.get_by_test_id("add-2").click()
    page.get_by_test_id("add-5").click()
    expect(page.get_by_test_id("cart-count")).to_have_text("3")


def test_checkout_and_pay(page: Page):
    open_shop(page)
    sign_in(page)
    order_id = place_order(page, [1, 3])
    expect(page.get_by_test_id(f"status-{order_id}")).to_have_text("created")
    page.get_by_test_id(f"pay-{order_id}").click()
    expect(page.get_by_test_id(f"status-{order_id}")).to_have_text("paid")


def test_cancel_discounted_order(page: Page):
    """Fails while planted bug 1 is active: the cancel request returns 500."""
    open_shop(page)
    sign_in(page)
    order_id = place_order(page, [4, 4], code="TILE25")
    page.get_by_test_id(f"pay-{order_id}").click()
    expect(page.get_by_test_id(f"status-{order_id}")).to_have_text("paid")
    page.get_by_test_id(f"cancel-{order_id}").click()
    expect(page.get_by_test_id("toast")).to_contain_text(f"Order {order_id} cancelled")


def test_checkout_needs_sign_in(page: Page):
    open_shop(page)
    page.get_by_test_id("add-1").click()
    page.get_by_test_id("checkout").click()
    expect(page.get_by_test_id("toast")).to_have_text("Sign in to place an order.")
