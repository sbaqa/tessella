"""Shared fixtures for the Mosaic Market suite.

- `shop_url` uses a running shop (DEMO_SHOP_URL, default http://127.0.0.1:8765) or starts one
  in a background thread for the session.
- Every test starts from an empty shop (orders and charges cleared).
- UI tests get a screenshot attached to the pytest-html report, which is the evidence
  Tessella shows next to a failure.
"""

from __future__ import annotations

import base64
import os
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.helpers import login

ROOT = Path(__file__).resolve().parent.parent
HOST, PORT = "127.0.0.1", 8765
SHOTS = ROOT / "test-results" / "screenshots"


def _is_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/health", timeout=0.5).status_code == 200
    except httpx.HTTPError:
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


@pytest.fixture(autouse=True)
def _empty_shop(shop_url):
    httpx.post(f"{shop_url}/api/test/reset", timeout=5).raise_for_status()


@pytest.fixture
def api(shop_url):
    with httpx.Client(base_url=shop_url, timeout=10) as client:
        yield client


@pytest.fixture
def anna(api):
    return login(api, "anna")


@pytest.fixture
def bob(api):
    return login(api, "bob")


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
