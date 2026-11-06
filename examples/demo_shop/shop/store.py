"""Shop logic, kept free of the web framework so it is easy to read and test.

The planted bugs are marked with `bugs.active(n)` so each one can be switched off.
"""

from __future__ import annotations

import itertools
import random
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field

from shop import bugs


class ShopError(Exception):
    status = 400


class BadRequest(ShopError):
    status = 400


class Unauthorized(ShopError):
    status = 401


class NotFound(ShopError):
    status = 404


class Conflict(ShopError):
    status = 409


# Test accounts for this demo only.
USERS = {"anna": "anna-pass", "bob": "bob-pass"}

PRODUCTS = [
    {"id": 1, "name": "Lapis Hexagon", "price": 1450, "colour": "#2f5fd0",
     "art": "conic-gradient(from 30deg, #1d3f99 0 60deg, #2f5fd0 0 120deg, #6e98f0 0 180deg, #1d3f99 0 240deg, #2f5fd0 0 300deg, #6e98f0 0)"},
    {"id": 2, "name": "Terracotta Square", "price": 980, "colour": "#c2551b",
     "art": "repeating-linear-gradient(0deg, #c2551b 0 22px, #e07a3f 22px 24px), repeating-linear-gradient(90deg, transparent 0 22px, #e07a3f 22px 24px)"},
    {"id": 3, "name": "Verdigris Fish Scale", "price": 1620, "colour": "#1f8a83",
     "art": "radial-gradient(circle at 50% 0, #4fc2b9 0 18px, #1f8a83 19px 30px, transparent 31px) 0 0 / 40px 30px, #16605b"},
    {"id": 4, "name": "Marble Arabesque", "price": 2490, "colour": "#8a8475",
     "art": "radial-gradient(circle at 25% 25%, #f4f1ea 0 18%, transparent 19%), radial-gradient(circle at 75% 75%, #f4f1ea 0 18%, transparent 19%), linear-gradient(135deg, #cfc8b8, #9d9584)"},
    {"id": 5, "name": "Saffron Penny Round", "price": 1190, "colour": "#d6a21e",
     "art": "radial-gradient(circle, #f3c94f 0 9px, #b4840f 10px 11px, transparent 12px) 0 0 / 24px 24px, #8a6309"},
    {"id": 6, "name": "Obsidian Chevron", "price": 1880, "colour": "#1b2430",
     "art": "linear-gradient(135deg, #2a3646 25%, transparent 25%) -20px 0 / 40px 40px, linear-gradient(225deg, #2a3646 25%, transparent 25%) -20px 0 / 40px 40px, #121a26"},
]
_BY_ID = {p["id"]: p for p in PRODUCTS}

DISCOUNTS = {"MOSAIC10": 0.10, "TILE25": 0.25}
# Share of the paid amount refunded on cancel, per discount code.
# Planted bug 1: the keys are lowercase but orders store codes in uppercase.
REFUND_RULES = {"mosaic10": 1.0, "tile25": 1.0}


@dataclass
class Order:
    id: int
    owner: str
    items: list[dict]
    subtotal: int
    discount_code: str | None
    total: int
    status: str = "created"  # created -> paid -> shipped, or cancelled
    refunded: int = 0

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Store:
    orders: dict[int, Order] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    charges: list[dict] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1001))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- accounts -----------------------------------------------------------------------
    def login(self, username: str, password: str) -> str:
        if USERS.get(username) != password:
            raise Unauthorized("wrong username or password")
        token = "tok-" + secrets.token_hex(12)
        self.tokens[token] = username
        return token

    def user_for(self, token: str | None) -> str:
        if not token or token not in self.tokens:
            raise Unauthorized("log in first")
        return self.tokens[token]

    # --- catalogue ----------------------------------------------------------------------
    def products(self) -> list[dict]:
        return PRODUCTS

    def recommendations(self) -> list[dict]:
        # A slow, uneven dependency: the source of the demo's flaky test.
        time.sleep(random.uniform(0.0, 1.2))
        return random.sample(PRODUCTS, 3)

    # --- orders -------------------------------------------------------------------------
    def create_order(self, user: str, items: list[dict], discount_code: str | None) -> Order:
        if not items:
            raise BadRequest("an order needs at least one item")
        lines, subtotal = [], 0
        for item in items:
            product = _BY_ID.get(item.get("product_id"))
            qty = item.get("qty")
            if product is None:
                raise BadRequest(f"unknown product {item.get('product_id')}")
            if not isinstance(qty, int) or not 1 <= qty <= 20:
                raise BadRequest("qty must be a whole number from 1 to 20")
            lines.append({"product_id": product["id"], "name": product["name"],
                          "qty": qty, "price": product["price"]})
            subtotal += product["price"] * qty
        code = discount_code.strip().upper() if discount_code else None
        if code and code not in DISCOUNTS:
            raise BadRequest(f"unknown discount code {discount_code}")
        total = round(subtotal * (1 - DISCOUNTS[code])) if code else subtotal
        order = Order(id=next(self._ids), owner=user, items=lines, subtotal=subtotal,
                      discount_code=code, total=total)
        self.orders[order.id] = order
        return order

    def _owned(self, user: str, order_id: int) -> Order:
        order = self.orders.get(order_id)
        if order is None or order.owner != user:
            raise NotFound(f"order {order_id} not found")
        return order

    def get_order(self, user: str, order_id: int) -> Order:
        if bugs.active(3):
            # Planted bug 3: no owner check, any user can read any order.
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(f"order {order_id} not found")
            return order
        return self._owned(user, order_id)

    def my_orders(self, user: str) -> list[Order]:
        return [o for o in self.orders.values() if o.owner == user]

    def pay(self, user: str, order_id: int) -> Order:
        order = self._owned(user, order_id)
        if bugs.active(4):
            # Planted bug 4: check-then-act without a lock. Two parallel requests both
            # see "created" before either marks the order paid, so both charge.
            if order.status != "created":
                raise Conflict(f"order {order_id} is {order.status}")
            time.sleep(0.05)  # the payment provider call
            self.charges.append({"order_id": order.id, "amount": order.total})
            order.status = "paid"
            return order
        with self._lock:
            if order.status != "created":
                raise Conflict(f"order {order_id} is {order.status}")
            time.sleep(0.05)
            self.charges.append({"order_id": order.id, "amount": order.total})
            order.status = "paid"
        return order

    def ship(self, user: str, order_id: int) -> Order:
        order = self._owned(user, order_id)
        if order.status != "paid":
            raise Conflict(f"only paid orders ship; order {order_id} is {order.status}")
        order.status = "shipped"
        return order

    def cancel(self, user: str, order_id: int) -> Order:
        order = self._owned(user, order_id)
        if order.status == "cancelled":
            raise Conflict(f"order {order_id} is already cancelled")
        was = order.status
        if bugs.active(5):
            # Planted bug 5: the status changes before the rule is checked, so a rejected
            # cancel still cancels the order.
            order.status = "cancelled"
            if was == "shipped":
                raise Conflict("shipped orders cannot be cancelled")
        else:
            if was == "shipped":
                raise Conflict("shipped orders cannot be cancelled")
            order.status = "cancelled"

        if was == "paid":
            if order.discount_code:
                if bugs.active(1):
                    # Planted bug 1: KeyError -> 500 for every discounted order.
                    share = REFUND_RULES[order.discount_code]
                else:
                    share = REFUND_RULES[order.discount_code.lower()]
            else:
                share = 1.0
            order.refunded = round(order.total * share)
        return order

    def charges_for(self, user: str, order_id: int) -> list[dict]:
        self._owned(user, order_id)
        return [c for c in self.charges if c["order_id"] == order_id]

    def reset(self) -> None:
        self.orders.clear()
        self.charges.clear()
        self._ids = itertools.count(1001)
