"""The personas. Each quirk test checks what a correct shop does, so it FAILS while the
persona's quirk is on: they are marked `persona` and left out of the default run, to keep
the default run's failures exactly the planted ones.

    pytest -m persona        # see what each persona breaks
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.framework.data import LAPIS, OBSIDIAN, TERRACOTTA, VERDIGRIS, Delivery, money

pytestmark = pytest.mark.ui


def test_standard_persona_shops_normally(shopper):
    shopper.sign_in("standard")
    assert shopper.buy(LAPIS) >= 1001


def test_slow_persona_still_gets_in(shopper):
    expect.set_options(timeout=6000)
    try:
        shopper.sign_in("slow")
    finally:
        expect.set_options(timeout=3000)


@pytest.mark.persona
def test_problem_persona_sees_each_patterns_own_image(shopper):
    inventory = shopper.sign_in("problem")
    lapis = inventory.page.locator(f"[data-testid=product-{LAPIS.id}] .swatch").get_attribute("style")
    obsidian = inventory.page.locator(f"[data-testid=product-{OBSIDIAN.id}] .swatch").get_attribute("style")
    assert lapis != obsidian


@pytest.mark.persona
def test_problem_persona_can_sort(shopper):
    inventory = shopper.sign_in("problem")
    inventory.sort_by("hilo")
    assert inventory.prices() == sorted(inventory.prices(), reverse=True)


@pytest.mark.persona
def test_problem_persona_can_add_every_pattern(shopper):
    inventory = shopper.sign_in("problem")
    inventory.add(TERRACOTTA)


@pytest.mark.persona
def test_problem_persona_can_type_a_last_name(shopper, cart):
    shopper.sign_in("problem")
    shopper.fill_cart(LAPIS)
    cart.header.open_cart()
    info = cart.checkout().fill(Delivery())
    expect(info.last_name).to_have_value("Tiler")


@pytest.mark.persona
def test_error_persona_can_remove_from_the_cart(shopper, cart):
    shopper.sign_in("error")
    shopper.fill_cart(LAPIS)
    cart.header.open_cart()
    cart.remove(LAPIS)
    expect(cart.empty).to_be_visible()


@pytest.mark.persona
def test_error_persona_can_place_an_order(shopper):
    shopper.sign_in("error")
    shopper.buy(LAPIS)


@pytest.mark.persona
def test_visual_persona_sees_the_right_price(shopper):
    inventory = shopper.sign_in("visual")
    expect(inventory.page.get_by_test_id(f"price-{VERDIGRIS.id}")).to_contain_text(money(VERDIGRIS.price))
