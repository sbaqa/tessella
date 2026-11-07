"""Signing in and out in the browser.

test_login is the planted test bug (bug 2): it was written before the page objects and
still clicks #login, but the button is now #sign-in. The shop works; the test is wrong.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.framework.data import PASSWORDS
from tests.framework.pages.screens import InventoryPage

pytestmark = pytest.mark.ui


def test_login(page: Page):
    """Planted bug 2 (test bug): the button id changed from #login to #sign-in."""
    page.goto("/")
    expect(page.locator("body[data-ready=true]")).to_be_attached()
    page.fill("#username", "anna")
    page.fill("#password", PASSWORDS["anna"])
    page.click("#login", timeout=3000)
    expect(page.locator("#who")).to_have_text("Signed in as anna")


def test_sign_in_shows_the_patterns(shopper):
    inventory = shopper.sign_in("anna")
    expect(inventory.heading()).to_have_text("Glazed tiles, sold by the sheet.")
    expect(inventory.tiles).to_have_count(6)


def test_wrong_password_shows_an_error_that_can_be_dismissed(login_page):
    login_page.open().sign_in("anna", "not-her-password")
    login_page.expect_error("Username and password do not match any user in this shop")
    expect(login_page.username).to_have_attribute("aria-invalid", "true")
    login_page.dismiss_error()
    expect(login_page.username).not_to_have_attribute("aria-invalid", "true")


@pytest.mark.parametrize("user,password,message", [("", "x", "Username is required"),
                                                   ("anna", "", "Password is required")])
def test_sign_in_checks_empty_fields(login_page, user, password, message):
    login_page.open().sign_in(user, password)
    login_page.expect_error(message)


def test_locked_user_is_told_they_are_locked_out(login_page):
    login_page.open().sign_in("locked", PASSWORDS["locked"])
    login_page.expect_error("Sorry, this user has been locked out")


def test_picking_a_test_account_fills_the_form(login_page):
    login_page.open().pick_account("bob")
    expect(login_page.username).to_have_value("bob")
    expect(login_page.password).to_have_value(PASSWORDS["bob"])


@pytest.mark.parametrize("path", ["/inventory", "/cart", "/checkout/info", "/orders"])
def test_screens_need_a_signed_in_user(page, path):
    page.goto(f"/#{path}")
    expect(page).to_have_url(f"{page.url.split('#')[0]}#/login")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()


def test_session_survives_a_reload(shopper, page):
    shopper.sign_in("anna")
    page.reload()
    inventory = InventoryPage(page).expect_loaded()
    inventory.header.expect_signed_in_as("anna")


def test_log_out_returns_to_sign_in(shopper, page):
    inventory = shopper.sign_in("anna")
    inventory.menu.open().go("logout")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    expect(page.locator("#who")).to_have_text("Not signed in")
    page.goto("/#/inventory")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
