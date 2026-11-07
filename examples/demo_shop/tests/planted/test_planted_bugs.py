"""Proof tests for the planted product bugs, on Playwright's API client.

These are NOT part of the normal suite (pytest.ini deselects the `planted` marker). They
exist so you can check that each bug is really there, and really gone once fixed:

    pytest -m planted                          # all four fail while the bugs are active
    DEMO_SHOP_FIXED=all pytest -m planted      # all four pass

Bugs 3, 4 and 5 are the ones a normal suite misses. They are what Tessella's probe step
is for: identity swaps, parallel replays and state checks after a rejected request.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from playwright.async_api import async_playwright

from tests.framework.data import OBSIDIAN, TERRACOTTA

pytestmark = pytest.mark.planted


def test_bug1_cancel_discounted_order(orders, order_steps):
    order = order_steps.paid(TERRACOTTA.id, 3, code="TILE25")
    res = orders.cancel(order["id"])
    assert res.status == 200, f"bug 1 active: cancel returned {res.status}"


def test_bug3_other_user_cannot_read_order(bob_orders, order_steps):
    order = order_steps.created()
    res = bob_orders.get(order["id"])
    assert res.status == 404, "bug 3 active: bob can read anna's order"


def _pay_in_parallel(shop_url: str, token: str, order_id: int, n: int = 4) -> list[int]:
    """n pay requests at once. Playwright's sync API is single-threaded, so the burst runs
    on the async API in a thread of its own."""
    async def burst():
        async with async_playwright() as pw:
            ctx = await pw.request.new_context(base_url=shop_url)
            res = await asyncio.gather(*(ctx.post(f"/api/orders/{order_id}/pay",
                                                  headers={"Authorization": f"Bearer {token}"})
                                         for _ in range(n)))
            codes = [r.status for r in res]
            await ctx.dispose()
            return codes

    out: list[int] = []
    t = threading.Thread(target=lambda: out.extend(asyncio.run(burst())))
    t.start()
    t.join(timeout=30)
    return sorted(out)


def test_bug4_parallel_pay_charges_once(shop_url, orders, order_steps):
    order = order_steps.created(OBSIDIAN.id, 1)
    codes = _pay_in_parallel(shop_url, orders.token, order["id"])
    charges = orders.charges(order["id"]).json()
    assert len(charges) == 1, f"bug 4 active: {len(charges)} charges, responses {codes}"


def test_bug5_rejected_cancel_leaves_order_shipped(orders, order_steps):
    order = order_steps.shipped()
    assert orders.cancel(order["id"]).status == 409
    after = orders.get(order["id"]).json()
    assert after["status"] == "shipped", f"bug 5 active: order is now {after['status']}"
