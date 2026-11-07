"""Parts every signed-in screen shares: the header, the side menu and the toast."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


class Header:
    def __init__(self, page: Page):
        self.page = page
        self.who: Locator = page.locator("#who")
        self.cart_count: Locator = page.get_by_test_id("cart-count")
        self.cart_link: Locator = page.get_by_test_id("cart-link")
        self.menu_button: Locator = page.get_by_test_id("menu-button")

    def open_cart(self) -> None:
        self.cart_link.click()

    def expect_signed_in_as(self, user: str) -> None:
        expect(self.who).to_have_text(f"Signed in as {user}")

    def expect_cart_count(self, n: int) -> None:
        expect(self.cart_count).to_have_text(str(n))


class SideMenu:
    def __init__(self, page: Page):
        self.page = page
        self.drawer: Locator = page.locator("#drawer")

    def open(self) -> SideMenu:
        self.page.get_by_test_id("menu-button").click()
        expect(self.drawer).to_have_attribute("aria-hidden", "false")
        return self

    def close_with_escape(self) -> None:
        self.page.keyboard.press("Escape")
        expect(self.drawer).to_have_attribute("aria-hidden", "true")

    def go(self, item: str) -> None:
        """item: all-items, orders, about, reset or logout."""
        self.page.get_by_test_id(f"menu-{item}").click()


class Toast:
    def __init__(self, page: Page):
        self.locator: Locator = page.get_by_test_id("toast")

    def expect_text(self, text: str) -> None:
        expect(self.locator).to_contain_text(text)

    def expect_error(self) -> None:
        expect(self.locator).to_have_attribute("data-kind", "error")

    def undo(self) -> None:
        self.locator.get_by_test_id("undo").click()
