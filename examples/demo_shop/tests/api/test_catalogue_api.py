"""API tests for the catalogue. The recommendations test is flaky on purpose."""

from __future__ import annotations

import time


def test_health(api):
    assert api.get("/api/health").json() == {"status": "ok"}


def test_products_are_listed(api):
    products = api.get("/api/products").json()
    assert len(products) == 6
    assert {"id", "name", "price"} <= products[0].keys()
    assert all(p["price"] > 0 for p in products)


def test_recommendations_respond_quickly(api):
    """Flaky: the recommendations service takes 0 to 1.2 s, and this budget is 0.8 s.

    It passes about two runs in three, so Tessella's re-run step should call it flaky,
    not a product bug.
    """
    start = time.perf_counter()
    res = api.get("/api/recommendations")
    elapsed = time.perf_counter() - start
    assert res.status_code == 200
    assert len(res.json()) == 3
    assert elapsed < 0.8, f"recommendations took {elapsed:.2f}s, budget is 0.80s"


def test_login_with_wrong_password_is_rejected(api):
    res = api.post("/api/auth/login", json={"username": "anna", "password": "not-her-password"})
    assert res.status_code == 401
