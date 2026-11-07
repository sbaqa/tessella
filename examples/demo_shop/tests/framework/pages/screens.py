"""Page objects: one class per screen of the shop.

Each knows its own screen's selectors and actions and nothing else (single
responsibility). Tests talk to these, never to selectors, so a markup change is fixed in
one place. Actions that land on another screen return that screen's object.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect

from tests.framework.data import Delivery, Product
from tests.framework.pages.components import Header, SideMenu, Toast


class Screen:
    """What every screen has: the page, the shared header, menu and toast."""

    path = "/"

    def __init__(self, page: Page):
        self.page = page
        self.header = Header(page)
        self.menu = SideMenu(page)
        self.toast = Toast(page)

    def open(self):
        self.page.goto(f"/#{self.path}")
        expect(self.page.locator("body[data-ready=true]")).to_be_attached()
        return self

    def heading(self) -> Locator:
        return self.page.get_by_role("heading", level=1)


class LoginPage(Screen):
    path = "/login"

    def __init__(self, page: Page):
        super().__init__(page)
        self.username = page.get_by_label("Username")
        self.password = page.get_by_label("Password")
        self.submit = page.get_by_role("button", name="Sign in")
        self.error = page.get_by_test_id("login-error")

    def sign_in(self, user: str, password: str) -> None:
        self.username.fill(user)
        self.password.fill(password)
        self.submit.click()

    def pick_account(self, user: str) -> None:
        self.page.locator(f"[data-fill='{user}']").click()

    def expect_error(self, text: str) -> None:
        expect(self.error).to_be_visible()
        expect(self.error).to_contain_text(text)

    def dismiss_error(self) -> None:
        self.error.get_by_role("button", name="Dismiss").click()
        expect(self.error).to_be_hidden()


class InventoryPage(Screen):
    path = "/inventory"

    def __init__(self, page: Page):
        super().__init__(page)
        self.tiles = page.get_by_test_id(re.compile(r"^product-\d+$"))
        self.sort = page.get_by_test_id("sort")
        self.search = page.get_by_test_id("search")
        self.no_results = page.get_by_test_id("no-results")
        self.recommendations = page.get_by_test_id("recommendations")

    def expect_loaded(self) -> InventoryPage:
        expect(self.tiles.first).to_be_visible()
        return self

    def add(self, product: Product) -> None:
        self.page.get_by_test_id(f"add-{product.id}").click()
        expect(self.page.get_by_test_id(f"remove-{product.id}")).to_be_visible()

    def remove(self, product: Product) -> None:
        self.page.get_by_test_id(f"remove-{product.id}").click()

    def names(self) -> list[str]:
        return self.page.locator(".tile-name").all_inner_texts()

    def prices(self) -> list[int]:
        texts = self.page.get_by_test_id(re.compile(r"^price-\d+$")).all_inner_texts()
        return [round(float(re.search(r"\$([\d.]+)", t).group(1)) * 100) for t in texts]

    def sort_by(self, key: str) -> None:
        """az, za, lohi or hilo."""
        self.sort.select_option(key)

    def open_product(self, product: Product) -> ProductPage:
        self.page.get_by_test_id(f"name-{product.id}").click()
        return ProductPage(self.page).expect_loaded(product)


class ProductPage(Screen):
    def expect_loaded(self, product: Product) -> ProductPage:
        expect(self.page.get_by_test_id("product-detail")).to_be_visible()
        expect(self.heading()).to_have_text(product.name)
        return self

    def add(self, product: Product) -> None:
        self.page.get_by_test_id(f"add-{product.id}").click()

    def back(self) -> InventoryPage:
        self.page.get_by_test_id("back-to-products").click()
        return InventoryPage(self.page).expect_loaded()


class CartPage(Screen):
    path = "/cart"

    def __init__(self, page: Page):
        super().__init__(page)
        self.empty = page.get_by_test_id("cart-empty")

    def line(self, product: Product) -> Locator:
        return self.page.get_by_test_id(f"cart-line-{product.id}")

    def quantity(self, product: Product) -> Locator:
        return self.page.get_by_test_id(f"qty-{product.id}")

    def increase(self, product: Product) -> None:
        self.page.locator(f"[data-inc='{product.id}']").click()

    def decrease(self, product: Product) -> None:
        self.page.locator(f"[data-dec='{product.id}']").click()

    def remove(self, product: Product) -> None:
        self.line(product).get_by_test_id(f"remove-{product.id}").click()

    def checkout(self) -> CheckoutInfoPage:
        self.page.get_by_test_id("checkout").click()
        return CheckoutInfoPage(self.page)

    def continue_shopping(self) -> InventoryPage:
        self.page.get_by_test_id("continue-shopping").click()
        return InventoryPage(self.page).expect_loaded()


class CheckoutInfoPage(Screen):
    path = "/checkout/info"

    def __init__(self, page: Page):
        super().__init__(page)
        self.first_name = page.get_by_label("First name")
        self.last_name = page.get_by_label("Last name")
        self.postal_code = page.get_by_label("Postal code")
        self.error = page.get_by_test_id("checkout-error")

    def fill(self, delivery: Delivery) -> CheckoutInfoPage:
        self.first_name.fill(delivery.first_name)
        self.last_name.fill(delivery.last_name)
        self.postal_code.fill(delivery.postal_code)
        return self

    def submit(self) -> None:
        self.page.get_by_test_id("continue").click()

    def continue_to_overview(self, delivery: Delivery) -> CheckoutOverviewPage:
        self.fill(delivery).submit()
        return CheckoutOverviewPage(self.page).expect_loaded()

    def expect_error(self, text: str) -> None:
        expect(self.error).to_be_visible()
        expect(self.error).to_contain_text(text)


class CheckoutOverviewPage(Screen):
    path = "/checkout/overview"

    def __init__(self, page: Page):
        super().__init__(page)
        self.item_total = page.get_by_test_id("item-total")
        self.total = page.get_by_test_id("total")
        self.discount = page.get_by_test_id("discount")

    def expect_loaded(self) -> CheckoutOverviewPage:
        expect(self.heading()).to_have_text("Overview")
        return self

    def apply_discount(self, code: str) -> None:
        self.discount.fill(code)
        self.page.get_by_test_id("apply-discount").click()

    def place_order(self) -> CheckoutCompletePage:
        self.page.get_by_test_id("finish").click()
        return CheckoutCompletePage(self.page)

    def cancel(self) -> CartPage:
        self.page.get_by_test_id("cancel-overview").click()
        return CartPage(self.page)


class CheckoutCompletePage(Screen):
    def order_number(self) -> int:
        number = self.page.get_by_test_id("order-number")
        expect(number).to_be_visible()
        return int(number.inner_text())


class OrdersPage(Screen):
    path = "/orders"

    def __init__(self, page: Page):
        super().__init__(page)
        self.empty = page.get_by_test_id("orders-empty")

    def status(self, order_id: int) -> Locator:
        return self.page.get_by_test_id(f"status-{order_id}")

    def pay(self, order_id: int) -> None:
        self.page.get_by_test_id(f"pay-{order_id}").click()

    def cancel(self, order_id: int) -> None:
        self.page.get_by_test_id(f"cancel-{order_id}").click()
