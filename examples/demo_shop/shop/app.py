"""HTTP layer of Mosaic Market (Starlette, the toolkit FastAPI is built on).

Run it:  uvicorn shop.app:app --port 8765

Every request is written to logs/app.log (DEMO_SHOP_LOG overrides the path), with the
full traceback when a request crashes. Tessella reads that file as backend evidence.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from shop.store import ShopError, Store

STATIC = Path(__file__).parent / "static"
LOG_PATH = Path(os.environ.get("DEMO_SHOP_LOG", Path(__file__).parent.parent / "logs" / "app.log"))
store = Store()

log = logging.getLogger("shop")
if not log.handlers:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class RequestLog(BaseHTTPMiddleware):
    """One line per API request; the traceback too when a request crashes."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        line = f"{request.method} {request.url.path}"
        try:
            response = await call_next(request)
        except Exception:
            log.exception("%s -> 500 unhandled error", line)
            raise
        if request.url.path.startswith("/api/") and request.url.path != "/api/test/reset":
            ms = (time.perf_counter() - start) * 1000
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            log.log(level, "%s -> %s in %.0f ms", line, response.status_code, ms)
        return response


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header.removeprefix("Bearer ").strip() or None


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _user(request: Request) -> str:
    return store.user_for(_token(request))


async def index(request: Request):
    return FileResponse(STATIC / "index.html")


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def products(request: Request):
    return JSONResponse(store.products())


async def recommendations(request: Request):
    return JSONResponse(await run_in_threadpool(store.recommendations))


async def login(request: Request):
    body = await _body(request)
    token = store.login(str(body.get("username", "")), str(body.get("password", "")))
    return JSONResponse({"token": token, "username": body.get("username")})


async def list_orders(request: Request):
    user = _user(request)
    return JSONResponse([o.public() for o in store.my_orders(user)])


async def create_order(request: Request):
    user = _user(request)
    body = await _body(request)
    order = store.create_order(user, body.get("items") or [], body.get("discount_code"))
    return JSONResponse(order.public(), status_code=201)


async def get_order(request: Request):
    user = _user(request)
    return JSONResponse(store.get_order(user, request.path_params["order_id"]).public())


async def pay(request: Request):
    user = _user(request)
    order = await run_in_threadpool(store.pay, user, request.path_params["order_id"])
    return JSONResponse(order.public())


async def ship(request: Request):
    user = _user(request)
    return JSONResponse(store.ship(user, request.path_params["order_id"]).public())


async def cancel(request: Request):
    user = _user(request)
    return JSONResponse(store.cancel(user, request.path_params["order_id"]).public())


async def charges(request: Request):
    user = _user(request)
    return JSONResponse(store.charges_for(user, request.path_params["order_id"]))


async def reset(request: Request):
    """Test helper: empty the shop between tests. A real shop would not expose this."""
    store.reset()
    return JSONResponse({"reset": True})


async def shop_error(request: Request, exc: ShopError):
    log.warning("%s %s rejected: %s", request.method, request.url.path, exc)
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/products", products),
    Route("/api/recommendations", recommendations),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/orders", list_orders, methods=["GET"]),
    Route("/api/orders", create_order, methods=["POST"]),
    Route("/api/orders/{order_id:int}", get_order),
    Route("/api/orders/{order_id:int}/pay", pay, methods=["POST"]),
    Route("/api/orders/{order_id:int}/ship", ship, methods=["POST"]),
    Route("/api/orders/{order_id:int}/cancel", cancel, methods=["POST"]),
    Route("/api/orders/{order_id:int}/charges", charges),
    Route("/api/test/reset", reset, methods=["POST"]),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(RequestLog)],
    exception_handlers={ShopError: shop_error},
)
