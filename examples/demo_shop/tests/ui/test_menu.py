"""The side menu: navigation, about, reset and keyboard handling."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import LAPIS, OBSIDIAN

pytestmark = pytest.mark.ui


def test_menu_opens_and_closes_with_escape(shopper):
    inventory = shopper.sign_in()
    inventory.menu.open()
    expect(inventory.header.menu_button).to_have_attribute("aria-expanded", "true")
    inventory.menu.close_with_escape()
    expect(inventory.header.menu_button).to_have_attribute("aria-expanded", "false")


def test_about_lists_the_personas(shopper, page):
    inventory = shopper.sign_in()
    inventory.menu.open().go("about")
    expect(page.get_by_role("heading", name="About Mosaic Market")).to_be_visible()
    for persona in ("standard", "locked", "problem", "slow", "error", "visual"):
        expect(page.locator("dt", has_text=persona)).to_be_visible()


def test_reset_app_state_empties_the_cart(shopper):
    inventory = shopper.sign_in()
    shopper.fill_cart(LAPIS, OBSIDIAN)
    inventory.menu.open().go("reset")
    inventory.toast.expect_text("App state reset")
    inventory.header.expect_cart_count(0)


def test_all_items_returns_to_the_patterns(shopper, cart):
    shopper.sign_in()
    cart.open()
    cart.menu.open().go("all-items")
    expect(cart.page.get_by_test_id("product-1")).to_be_visible()
