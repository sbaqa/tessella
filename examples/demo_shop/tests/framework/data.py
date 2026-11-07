"""Test data: accounts, products and delivery details, in one place.

Tests ask for data by meaning (a customer, a discounted order) instead of repeating raw
values, so a change to the shop's data is a change here only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PERSONA_PASSWORD = "mosaic-pass"

# Passwords come from the environment when set, matching the identities in tessella.yaml.
PASSWORDS = {
    "anna": os.environ.get("A_PASS", "anna-pass"),
    "bob": os.environ.get("B_PASS", "bob-pass"),
    **{p: PERSONA_PASSWORD for p in ("standard", "locked", "problem", "slow", "error", "visual")},
}


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    price: int  # cents per sheet


LAPIS = Product(1, "Lapis Hexagon", 1450)
TERRACOTTA = Product(2, "Terracotta Square", 980)
VERDIGRIS = Product(3, "Verdigris Fish Scale", 1620)
MARBLE = Product(4, "Marble Arabesque", 2490)
SAFFRON = Product(5, "Saffron Penny Round", 1190)
OBSIDIAN = Product(6, "Obsidian Chevron", 1880)
ALL_PRODUCTS = (LAPIS, TERRACOTTA, VERDIGRIS, MARBLE, SAFFRON, OBSIDIAN)

DISCOUNTS = {"MOSAIC10": 0.10, "TILE25": 0.25}


@dataclass(frozen=True)
class Delivery:
    first_name: str = "Anna"
    last_name: str = "Tiler"
    postal_code: str = "10115"

    def as_json(self) -> dict:
        return {"first_name": self.first_name, "last_name": self.last_name,
                "postal_code": self.postal_code}


def money(cents: int) -> str:
    """How the shop prints a price."""
    return f"${cents / 100:.2f}"


def discounted(cents: int, code: str | None) -> int:
    return round(cents * (1 - DISCOUNTS[code.upper()])) if code else cents
