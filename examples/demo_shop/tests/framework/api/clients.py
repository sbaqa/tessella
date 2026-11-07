"""One small client per area of the shop's API. Each returns Playwright responses, so a test
decides what a status code means; a client never asserts."""

from __future__ import annotations

from playwright.sync_api import APIResponse

from tests.framework.api.session import ApiSession
from tests.framework.data import PASSWORDS, Delivery


class AuthApi:
    def __init__(self, session: ApiSession):
        self._session = session

    def login(self, username: str, password: str) -> APIResponse:
        return self._session.post("/api/auth/login", {"username": username, "password": password})

    def signed_in(self, username: str) -> ApiSession:
        """A session signed in as `username`; fails loudly if the shop refuses the account."""
        res = self.login(username, PASSWORDS[username])
        if not res.ok:
            raise AssertionError(f"could not sign in as {username}: {res.status} {res.text()}")
        return self._session.as_user(res.json()["token"])


class CatalogueApi:
    def __init__(self, session: ApiSession):
        self._session = session

    def health(self) -> APIResponse:
        return self._session.get("/api/health")

    def products(self) -> APIResponse:
        return self._session.get("/api/products")

    def recommendations(self) -> APIResponse:
        return self._session.get("/api/recommendations")


class OrdersApi:
    def __init__(self, session: ApiSession):
        self._session = session

    @property
    def token(self) -> str | None:
        """The signed-in user's token, for tests that must send requests another way."""
        return self._session.token

    def create(self, items: list[dict], discount_code: str | None = None,
               ship_to: Delivery | dict | None = None) -> APIResponse:
        body: dict = {"items": items, "discount_code": discount_code}
        if ship_to is not None:
            body["ship_to"] = ship_to.as_json() if isinstance(ship_to, Delivery) else ship_to
        return self._session.post("/api/orders", body)

    def list(self) -> APIResponse:
        return self._session.get("/api/orders")

    def get(self, order_id: int) -> APIResponse:
        return self._session.get(f"/api/orders/{order_id}")

    def pay(self, order_id: int) -> APIResponse:
        return self._session.post(f"/api/orders/{order_id}/pay")

    def ship(self, order_id: int) -> APIResponse:
        return self._session.post(f"/api/orders/{order_id}/ship")

    def cancel(self, order_id: int) -> APIResponse:
        return self._session.post(f"/api/orders/{order_id}/cancel")

    def charges(self, order_id: int) -> APIResponse:
        return self._session.get(f"/api/orders/{order_id}/charges")


class OrderSteps:
    """Common set-ups built from OrdersApi: an order in a given state. Separate from the
    client so the client stays a thin map of endpoints."""

    def __init__(self, orders: OrdersApi):
        self._orders = orders

    def created(self, product_id: int = 1, qty: int = 2, code: str | None = None) -> dict:
        res = self._orders.create([{"product_id": product_id, "qty": qty}], code)
        assert res.status == 201, res.text()
        return res.json()

    def paid(self, product_id: int = 1, qty: int = 2, code: str | None = None) -> dict:
        order = self.created(product_id, qty, code)
        res = self._orders.pay(order["id"])
        assert res.ok, res.text()
        return res.json()

    def shipped(self, product_id: int = 1, qty: int = 2) -> dict:
        order = self.paid(product_id, qty)
        res = self._orders.ship(order["id"])
        assert res.ok, res.text()
        return res.json()
