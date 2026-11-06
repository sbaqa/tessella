"""Small helpers shared by the tests."""

from __future__ import annotations

import os

import httpx

# Demo accounts. The env vars match the identities in tessella.yaml.
USERS = {
    "anna": os.environ.get("A_PASS", "anna-pass"),
    "bob": os.environ.get("B_PASS", "bob-pass"),
}


def login(client: httpx.Client, user: str = "anna") -> dict:
    """Log in through the API and return the auth header for that user."""
    res = client.post("/api/auth/login", json={"username": user, "password": USERS[user]})
    res.raise_for_status()
    return {"Authorization": f"Bearer {res.json()['token']}"}
