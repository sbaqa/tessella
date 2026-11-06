"""Probe: replay recorded flows to confirm verdicts and find bugs the tests missed.

Two uses of one engine:
- confirm: replay a failed test's request chain without the test code, then shrink it.
- explore: link IDs between steps, mutate step N while replaying 1..N-1 for real,
  as other identities, out of order and in parallel; judge with oracles.
"""
