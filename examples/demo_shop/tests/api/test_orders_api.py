"""Orders over the API: pricing, validation, the order life cycle and ownership.

test_cancel_paid_discounted_order_refunds_in_full fails while planted bug 1 is active.
Bugs 3, 4 and 5 are covered in tests/planted, outside the default run.
"""

from __future__ import annotations

import pytest

from tests.framework.data import LAPIS, MARBLE, OBSIDIAN, TERRACOTTA, Delivery, discounted


# --- creating orders -----------------------------------------------------------------------

def test_new_order_is_created_with_its_total(orders):
    res = orders.create([{"product_id": LAPIS.id, "qty": 2}, {"product_id": TERRACOTTA.id, "qty": 1}])
    assert res.status == 201
    order = res.json()
    assert order["status"] == "created"
    assert order["owner"] == "anna"
    assert order["subtotal"] == order["total"] == 2 * LAPIS.price + TERRACOTTA.price
    assert [i["name"] for i in order["items"]] == [LAPIS.name, TERRACOTTA.name]


@pytest.mark.parametrize("typed", ["TILE25", "tile25", "  Tile25 "])
def test_discount_code_is_applied_in_any_case(orders, typed):
    order = orders.create([{"product_id": MARBLE.id, "qty": 2}], typed).json()
    assert order["discount_code"] == "TILE25"
    assert order["total"] == discounted(2 * MARBLE.price, "TILE25")


def test_unknown_discount_code_is_rejected(orders):
    res = orders.create([{"product_id": LAPIS.id, "qty": 1}], "FREESTUFF")
    assert res.status == 400
    assert "unknown discount code" in res.json()["error"]


@pytest.mark.parametrize("items,error", [
    ([], "at least one item"),
    ([{"product_id": 99, "qty": 1}], "unknown product 99"),
    ([{"product_id": 1, "qty": 0}], "qty must be"),
    ([{"product_id": 1, "qty": 21}], "qty must be"),
    ([{"product_id": 1, "qty": "2"}], "qty must be"),
])
def test_invalid_items_are_rejected(orders, items, error):
    res = orders.create(items)
    assert res.status == 400
    assert error in res.json()["error"]


def test_delivery_details_are_stored(orders):
    order = orders.create([{"product_id": LAPIS.id, "qty": 1}], ship_to=Delivery()).json()
    assert order["ship_to"] == Delivery().as_json()


@pytest.mark.parametrize("missing,message", [("first_name", "First name"), ("last_name", "Last name"),
                                             ("postal_code", "Postal code")])
def test_delivery_details_must_be_complete(orders, missing, message):
    ship_to = {**Delivery().as_json(), missing: "  "}
    res = orders.create([{"product_id": LAPIS.id, "qty": 1}], ship_to=ship_to)
    assert res.status == 400
    assert res.json()["error"] == f"{message} is required"


def test_each_user_lists_only_their_own_orders(orders, bob_orders, order_steps):
    mine = order_steps.created()
    bob_orders.create([{"product_id": OBSIDIAN.id, "qty": 1}])
    assert [o["id"] for o in orders.list().json()] == [mine["id"]]


# --- paying and shipping -------------------------------------------------------------------

def test_paying_charges_the_total_once(orders, order_steps):
    order = order_steps.created(LAPIS.id, 3)
    paid = orders.pay(order["id"])
    assert paid.status == 200 and paid.json()["status"] == "paid"
    assert orders.charges(order["id"]).json() == [{"order_id": order["id"], "amount": 3 * LAPIS.price}]


def test_a_paid_order_cannot_be_paid_again(orders, order_steps):
    order = order_steps.paid()
    res = orders.pay(order["id"])
    assert res.status == 409
    assert len(orders.charges(order["id"]).json()) == 1


def test_only_paid_orders_ship(orders, order_steps):
    order = order_steps.created()
    assert orders.ship(order["id"]).status == 409
    orders.pay(order["id"])
    shipped = orders.ship(order["id"])
    assert shipped.status == 200 and shipped.json()["status"] == "shipped"


def test_nobody_can_pay_someone_elses_order(bob_orders, order_steps):
    order = order_steps.created()
    assert bob_orders.pay(order["id"]).status == 404


def test_charges_are_private(bob_orders, order_steps):
    order = order_steps.paid()
    assert bob_orders.charges(order["id"]).status == 404


def test_unknown_order_is_not_found(orders):
    assert orders.get(424242).status == 404


# --- cancelling ----------------------------------------------------------------------------

def test_cancel_unpaid_order(orders, order_steps):
    order = order_steps.created()
    res = orders.cancel(order["id"])
    assert res.status == 200
    assert res.json()["status"] == "cancelled"
    assert res.json()["refunded"] == 0


def test_cancel_paid_order_refunds_in_full(orders, order_steps):
    order = order_steps.paid()
    res = orders.cancel(order["id"])
    assert res.status == 200
    assert res.json()["refunded"] == order["total"]


def test_cancel_paid_discounted_order_refunds_in_full(orders, order_steps):
    """Fails while bug 1 is active: the refund lookup crashes with a 500."""
    order = order_steps.paid(LAPIS.id, 2, code="MOSAIC10")
    res = orders.cancel(order["id"])
    assert res.status == 200, f"cancel returned {res.status}: {res.text()}"
    assert res.json()["refunded"] == order["total"]


def test_a_cancelled_order_cannot_be_cancelled_again(orders, order_steps):
    order = order_steps.created()
    orders.cancel(order["id"])
    assert orders.cancel(order["id"]).status == 409


def test_shipped_order_cannot_be_cancelled(orders, order_steps):
    order = order_steps.shipped()
    res = orders.cancel(order["id"])
    assert res.status == 409
    # Note: this test does not check the order afterwards, which is why bug 5 slips through.
