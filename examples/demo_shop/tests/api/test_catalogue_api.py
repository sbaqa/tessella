"""Catalogue API. test_recommendations_respond_quickly is flaky on purpose."""

from __future__ import annotations

import time

from tests.framework.data import ALL_PRODUCTS


def test_health(catalogue):
    res = catalogue.health()
    assert res.status == 200
    assert res.json() == {"status": "ok"}


def test_products_are_listed(catalogue):
    products = catalogue.products().json()
    assert len(products) == 6
    assert {"id", "name", "price", "desc", "art"} <= products[0].keys()
    assert all(p["price"] > 0 for p in products)


def test_products_match_the_catalogue(catalogue):
    listed = {(p["id"], p["name"], p["price"]) for p in catalogue.products().json()}
    assert listed == {(p.id, p.name, p.price) for p in ALL_PRODUCTS}


def test_product_ids_are_unique(catalogue):
    ids = [p["id"] for p in catalogue.products().json()]
    assert len(ids) == len(set(ids))


def test_recommendations_are_three_known_products(catalogue):
    picks = catalogue.recommendations().json()
    known = {p.id for p in ALL_PRODUCTS}
    assert len(picks) == 3
    assert len({p["id"] for p in picks}) == 3
    assert {p["id"] for p in picks} <= known


def test_recommendations_respond_quickly(catalogue):
    """Flaky: the recommendations service takes 0 to 1.2 s, and this budget is 0.8 s.

    It passes about two runs in three, so Tessella's re-run step should call it flaky,
    not a product bug.
    """
    start = time.perf_counter()
    res = catalogue.recommendations()
    elapsed = time.perf_counter() - start
    assert res.status == 200
    assert len(res.json()) == 3
    assert elapsed < 0.8, f"recommendations took {elapsed:.2f}s, budget is 0.80s"
