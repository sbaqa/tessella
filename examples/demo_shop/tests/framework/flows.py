"""Journeys made of several screens. Tests that need a signed-in shopper or a placed order
reuse these, so each test spells out only the step it is about."""

from __future__ import annotations

from playwright.sync_api import Page

from tests.framework.data import PASSWORDS, Delivery, Product
from tests.framework.pages.screens import CartPage, InventoryPage, LoginPage


class Shopper:
    def __init__(self, page: Page):
        self.page = page

    def sign_in(self, user: str = "anna") -> InventoryPage:
        login = LoginPage(self.page).open()
        login.sign_in(user, PASSWORDS[user])
        inventory = InventoryPage(self.page).expect_loaded()
        inventory.header.expect_signed_in_as(user)
        return inventory

    def fill_cart(self, *products: Product) -> InventoryPage:
        inventory = InventoryPage(self.page)
        for p in products:
            inventory.add(p)
        return inventory

    def buy(self, *products: Product, code: str | None = None,
            delivery: Delivery = Delivery()) -> int:
        """Place and pay an order from a signed-in session; returns its number. A product
        listed twice is bought twice (added once, then its quantity raised in the cart)."""
        unique = list(dict.fromkeys(products))
        inventory = self.fill_cart(*unique)
        inventory.header.open_cart()
        cart = CartPage(self.page)
        for p in unique:
            for _ in range(products.count(p) - 1):
                cart.increase(p)
        overview = cart.checkout().continue_to_overview(delivery)
        if code:
            overview.apply_discount(code)
        return overview.place_order().order_number()
