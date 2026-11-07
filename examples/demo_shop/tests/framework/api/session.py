"""The HTTP transport for API tests, on Playwright's APIRequestContext.

One job: send requests with the right base URL and token. The endpoint clients
(auth, catalogue, orders) build on it and never touch headers themselves.
"""

from __future__ import annotations

from playwright.sync_api import APIRequestContext, APIResponse


class ApiSession:
    def __init__(self, context: APIRequestContext, token: str | None = None):
        self._context = context
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path: str) -> APIResponse:
        return self._context.get(path, headers=self._headers())

    def post(self, path: str, json: dict | None = None) -> APIResponse:
        return self._context.post(path, headers=self._headers(), data=json if json is not None else {})

    def as_user(self, token: str) -> ApiSession:
        """The same transport, signed in as someone else."""
        return ApiSession(self._context, token)
