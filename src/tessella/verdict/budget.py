"""Step and token budgets per cluster, so a run can never run away (important in CI)."""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_steps: int = 8
    max_tokens: int = 50_000
    steps: int = 0
    tokens: int = 0

    def spend(self, tokens: int = 0) -> None:
        self.steps += 1
        self.tokens += tokens
        if self.steps > self.max_steps or self.tokens > self.max_tokens:
            raise BudgetExceeded(f"steps={self.steps} tokens={self.tokens}")
