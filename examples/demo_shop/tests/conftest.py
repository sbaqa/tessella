"""Shared fixtures for the Mosaic Market suite, all on Playwright.

- `shop_url` uses a running shop (DEMO_SHOP_URL, default http://127.0.0.1:8765) or starts one
  in a background thread for the session.
- Every test starts from an empty shop (orders and charges cleared).
- API tests get endpoint clients (`auth_api`, `catalogue`, `orders` for anna, `bob_orders`)
  built on Playwright's APIRequestContext; UI tests get page objects and a `shopper`.
  Tests depend on these, never on raw URLs or selectors.
- UI tests get a screenshot attached to the Allure and pytest-html reports, which is the
  evidence Tessella shows next to a failure.
"""

from __future__ import annotations

import base64
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Playwright, expect

from tests.framework.api.clients import AuthApi, CatalogueApi, OrdersApi, OrderSteps
from tests.framework.api.session import ApiSession
from tests.framework.flows import Shopper
from tests.framework.pages import screens

ROOT = Path(__file__).resolve().parent.parent
HOST, PORT = "127.0.0.1", 8765
SHOTS = ROOT / "test-results" / "screenshots"

expect.set_options(timeout=3000)


def _is_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=0.5) as res:
            return res.status == 200
    except OSError:
        return False


@pytest.fixture(scope="session")
def shop_url():
    url = os.environ.get("DEMO_SHOP_URL", f"http://{HOST}:{PORT}").rstrip("/")
    if _is_up(url):
        yield url
        return

    import uvicorn

    from shop.app import app

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not _is_up(url):
        if time.monotonic() > deadline:
            pytest.exit(f"the demo shop did not start on {url}", returncode=3)
        time.sleep(0.1)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def base_url(shop_url):
    """pytest-playwright reads this, so `page.goto("/")` opens the shop."""
    return shop_url


# --- API: one Playwright request context per test, clients on top ------------------------

@pytest.fixture
def request_context(playwright: Playwright, shop_url):
    context = playwright.request.new_context(base_url=shop_url, timeout=10_000)
    yield context
    context.dispose()


@pytest.fixture(autouse=True)
def _empty_shop(playwright: Playwright, shop_url):
    context = playwright.request.new_context(base_url=shop_url)
    assert context.post("/api/test/reset").ok
    context.dispose()


@pytest.fixture
def session(request_context) -> ApiSession:
    return ApiSession(request_context)


@pytest.fixture
def auth_api(session) -> AuthApi:
    return AuthApi(session)


@pytest.fixture
def catalogue(session) -> CatalogueApi:
    return CatalogueApi(session)


@pytest.fixture
def orders(auth_api) -> OrdersApi:
    """Orders API signed in as anna."""
    return OrdersApi(auth_api.signed_in("anna"))


@pytest.fixture
def bob_orders(auth_api) -> OrdersApi:
    return OrdersApi(auth_api.signed_in("bob"))


@pytest.fixture
def order_steps(orders) -> OrderSteps:
    return OrderSteps(orders)


# --- UI: page objects and the shopper journey --------------------------------------------

@pytest.fixture
def shopper(page) -> Shopper:
    return Shopper(page)


@pytest.fixture
def login_page(page) -> screens.LoginPage:
    return screens.LoginPage(page)


@pytest.fixture
def inventory(page) -> screens.InventoryPage:
    return screens.InventoryPage(page)


@pytest.fixture
def cart(page) -> screens.CartPage:
    return screens.CartPage(page)


@pytest.fixture
def orders_page(page) -> screens.OrdersPage:
    return screens.OrdersPage(page)


# --- screenshots into the test report (pytest-html and/or Allure) ------------------------

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    page = item.funcargs.get("page") if hasattr(item, "funcargs") else None
    if report.when != "call" or page is None:
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / (re.sub(r"[^\w.-]+", "_", item.nodeid).strip("_") + ".png")
    try:
        png = page.screenshot(path=str(path))
    except Exception:  # the page may already be closed; the test result still stands
        return
    try:
        from pytest_html import extras
    except ImportError:
        pass
    else:
        report.extras = getattr(report, "extras", []) + [
            extras.png(base64.b64encode(png).decode("ascii"), name="Screenshot at end of test")
        ]
    try:
        import allure
    except ImportError:
        pass
    else:
        allure.attach(png, name="Screenshot at end of test", attachment_type=allure.attachment_type.PNG)
