"""Pipette model: integer-nanolitre transfers bounded by min/max volume and step.

Every single transfer must be a multiple of ``step_nl`` and lie within
``[min_volume_nl, max_volume_nl]``.  Larger totals are achieved by splitting
into several valid transfers.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pipette:
    min_nl: int
    max_nl: int
    step_nl: int

    @property
    def lo(self) -> int:
        """Smallest transferable volume: min rounded up to a step multiple (>= one step)."""
        return max(self.step_nl, -(-self.min_nl // self.step_nl) * self.step_nl)

    @property
    def hi(self) -> int:
        """Largest transferable volume: max rounded down to a step multiple."""
        return (self.max_nl // self.step_nl) * self.step_nl

    def split(self, volume: int, hi: int | None = None) -> list[int] | None:
        """Split ``volume`` into valid single transfers.

        Returns the list of chunk volumes (each a multiple of step within
        ``[lo, hi]``) or ``None`` when the total cannot be realised.
        ``volume == 0`` yields an empty list.  ``hi`` may be lowered by the
        caller (e.g. to the capacity of the destination container).
        """
        if volume == 0:
            return []
        lo, st = self.lo, self.step_nl
        hi = self.hi if hi is None else min(hi, self.hi)
        if not self._feasible(volume, lo, hi, st):
            return None
        n = -(-volume // hi)
        chunks = [lo] * n
        remainder = volume - n * lo
        i = 0
        while remainder > 0:
            add = min(remainder, hi - chunks[i])
            chunks[i] += add
            remainder -= add
            i += 1
        return chunks

    @staticmethod
    def _feasible(volume: int, lo: int, hi: int, st: int) -> bool:
        """O(1) feasibility: exists n chunks with n*lo <= volume <= n*hi."""
        if volume % st != 0 or volume < lo or hi < lo:
            return False
        n = -(-volume // hi)
        return n * lo <= volume

    def transferable(self, volume: int, hi: int | None = None) -> bool:
        if volume == 0:
            return True
        hi = self.hi if hi is None else min(hi, self.hi)
        return self._feasible(volume, self.lo, hi, self.step_nl)
