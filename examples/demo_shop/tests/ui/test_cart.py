"""The cart: lines, quantities, removing with undo, and keeping it across reloads."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import LAPIS, OBSIDIAN, VERDIGRIS
from tests.framework.pages.screens import CartPage

pytestmark = pytest.mark.ui


def test_cart_lists_what_was_added(shopper, cart):
    shopper.sign_in()
    shopper.fill_cart(LAPIS, OBSIDIAN)
    cart.header.open_cart()
    expect(cart.line(LAPIS)).to_contain_text(LAPIS.name)
    expect(cart.line(OBSIDIAN)).to_contain_text(OBSIDIAN.name)
    expect(cart.quantity(LAPIS)).to_have_text("1")


def test_quantity_can_go_up_and_down(shopper, cart):
    shopper.sign_in()
    shopper.fill_cart(VERDIGRIS)
    cart.header.open_cart()
    cart.increase(VERDIGRIS)
    cart.increase(VERDIGRIS)
    expect(cart.quantity(VERDIGRIS)).to_have_text("3")
    cart.header.expect_cart_count(3)
    cart.decrease(VERDIGRIS)
    expect(cart.quantity(VERDIGRIS)).to_have_text("2")


def test_decreasing_to_zero_removes_the_line(shopper, cart):
    shopper.sign_in()
    shopper.fill_cart(LAPIS)
    cart.header.open_cart()
    cart.decrease(LAPIS)
    expect(cart.empty).to_be_visible()


def test_removing_a_line_can_be_undone(shopper, cart):
    shopper.sign_in()
    shopper.fill_cart(LAPIS, OBSIDIAN)
    cart.header.open_cart()
    cart.remove(LAPIS)
    expect(cart.line(LAPIS)).to_have_count(0)
    cart.toast.expect_text(f"{LAPIS.name} removed")
    cart.toast.undo()
    expect(cart.line(LAPIS)).to_be_visible()
    cart.header.expect_cart_count(2)


def test_cart_is_kept_after_a_reload(shopper, page):
    shopper.sign_in()
    shopper.fill_cart(OBSIDIAN)
    page.reload()
    cart = CartPage(page).open()
    expect(cart.line(OBSIDIAN)).to_be_visible()


def test_empty_cart_offers_a_way_back(shopper, cart):
    shopper.sign_in()
    cart.open()
    expect(cart.empty).to_be_visible()
    cart.page.get_by_role("link", name="Browse patterns").click()
    expect(cart.page.get_by_test_id("product-1")).to_be_visible()


def test_continue_shopping_returns_to_patterns(shopper, cart):
    shopper.sign_in()
    shopper.fill_cart(LAPIS)
    cart.header.open_cart()
    cart.continue_shopping()
