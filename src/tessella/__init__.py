"""Tessella: a local quality investigator for SDETs.

Every signal (a failed test from a report, or a finding from probing a flow)
becomes a Case that goes through one loop:
collect -> cluster/recall -> investigate -> probe -> verdict -> draft -> review.
"""

__version__ = "0.1.0"
