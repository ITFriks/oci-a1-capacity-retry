"""Deterministic exponential backoff (no jitter: timing must be predictable)."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Backoff:
    """Delay doubles on each advance() and is capped: 5, 10, 20, 40, 60, 60, ..."""

    initial: int
    maximum: int
    attempt: int = 0

    @property
    def delay(self) -> int:
        return min(self.initial * 2**self.attempt, self.maximum)

    def advance(self) -> Backoff:
        # Stop counting once capped so the exponent cannot grow without bound.
        if self.delay >= self.maximum:
            return self
        return replace(self, attempt=self.attempt + 1)

    def reset(self) -> Backoff:
        return replace(self, attempt=0)
