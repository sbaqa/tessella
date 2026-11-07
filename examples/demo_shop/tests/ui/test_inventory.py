"""The patterns screen: sorting, searching, adding to the cart, the product page."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import ALL_PRODUCTS, LAPIS, MARBLE, SAFFRON, TERRACOTTA, money
from tests.framework.pages.screens import InventoryPage

pytestmark = pytest.mark.ui

ORDERS = {
    "az": sorted(ALL_PRODUCTS, key=lambda p: p.name),
    "za": sorted(ALL_PRODUCTS, key=lambda p: p.name, reverse=True),
    "lohi": sorted(ALL_PRODUCTS, key=lambda p: p.price),
    "hilo": sorted(ALL_PRODUCTS, key=lambda p: p.price, reverse=True),
}


@pytest.mark.parametrize("key", ORDERS)
def test_sorting_orders_the_patterns(shopper, key):
    inventory = shopper.sign_in()
    inventory.sort_by(key)
    expect(inventory.page.locator(".tile-name")).to_have_text([p.name for p in ORDERS[key]])


def test_sort_order_is_remembered(shopper, page):
    inventory = shopper.sign_in()
    inventory.sort_by("hilo")
    page.reload()
    inventory = InventoryPage(page).expect_loaded()
    expect(inventory.sort).to_have_value("hilo")
    assert inventory.prices() == sorted(inventory.prices(), reverse=True)


def test_prices_are_shown_per_sheet(shopper):
    inventory = shopper.sign_in()
    inventory.sort_by("az")
    assert inventory.prices() == [p.price for p in ORDERS["az"]]
    expect(inventory.page.get_by_test_id(f"price-{LAPIS.id}")).to_contain_text(money(LAPIS.price))


def test_search_filters_by_name_and_description(shopper):
    inventory = shopper.sign_in()
    inventory.search.fill("marble")
    expect(inventory.tiles).to_have_count(1)
    expect(inventory.page.get_by_test_id(f"product-{MARBLE.id}")).to_be_visible()
    inventory.search.fill("corners")  # only in Saffron Penny Round's description
    expect(inventory.page.get_by_test_id(f"product-{SAFFRON.id}")).to_be_visible()
    expect(inventory.tiles).to_have_count(1)


def test_search_with_no_match_offers_to_clear(shopper):
    inventory = shopper.sign_in()
    inventory.search.fill("velvet")
    expect(inventory.no_results).to_contain_text("velvet")
    inventory.page.get_by_role("button", name="Clear the search").click()
    expect(inventory.tiles).to_have_count(6)


def test_adding_and_removing_updates_the_cart_badge(shopper):
    inventory = shopper.sign_in()
    inventory.header.expect_cart_count(0)
    inventory.add(TERRACOTTA)
    inventory.add(SAFFRON)
    inventory.header.expect_cart_count(2)
    inventory.remove(TERRACOTTA)
    inventory.header.expect_cart_count(1)
    expect(inventory.page.get_by_test_id(f"add-{TERRACOTTA.id}")).to_be_visible()


def test_product_page_shows_details_and_adds_to_cart(shopper):
    inventory = shopper.sign_in()
    product = inventory.open_product(MARBLE)
    expect(product.page.get_by_text("Honed marble")).to_be_visible()
    expect(product.page.get_by_text(money(MARBLE.price), exact=False).first).to_be_visible()
    product.add(MARBLE)
    product.header.expect_cart_count(1)
    product.back()


def test_recommendations_show_three_patterns(shopper):
    inventory = shopper.sign_in()
    expect(inventory.recommendations.locator("a.pick")).to_have_count(3, timeout=5000)
