"""Which planted bugs are active.

All bugs are on by default. Fix them with the DEMO_SHOP_FIXED environment variable:

    DEMO_SHOP_FIXED=all        every bug fixed
    DEMO_SHOP_FIXED=1,4        only bugs 1 and 4 fixed

The bugs (bug 2 is a test bug, so it lives in the tests, not here):

1. Cancelling a discounted order crashes with a 500 (refund rule lookup uses the wrong key).
3. Any logged-in user can read any order by its ID (no owner check).
4. Paying the same order twice in parallel charges twice (check-then-act without a lock).
5. Cancelling a shipped order is rejected with 409, but the order is cancelled anyway.
"""

from __future__ import annotations

import os

ALL = {1, 3, 4, 5}


def fixed() -> set[int]:
    raw = os.environ.get("DEMO_SHOP_FIXED", "").strip().lower()
    if raw == "all":
        return set(ALL)
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def active(number: int) -> bool:
    """True while planted bug `number` is switched on."""
    return number not in fixed()
