"""The ticket pool of one session, cell by cell.

The seating plan of a session is split into **cells** (one per area, optionally
one per ticket type).  A single request can only take tickets from **one**
cell; the seats of a whole order may come from several cells.

Conservation invariant, checked after every mutation:

    sum(capacity) == remaining + pending + paid

Port boundary: this is the **Python reference**; the Rust world core has the
same thing in ``core/world/src/session.rs`` (the pool lives inside ``Session``:
``sell`` / ``checkout`` / ``return_seats`` / ``pool_ok``).
The two must stay bit-identical (``tests/test_backends.py`` compares them
event by event), so a change to the mechanism goes into both.
"""
from __future__ import annotations

from typing import Dict, Iterable, Sequence

from .schema import CellKey, ResolvedCell


class CellPool:
    """Integer stock of a session, kept per cell."""

    __slots__ = ("capacity", "remaining", "pending", "paid", "total",
                 "_remaining_total", "_pending_total", "_paid_total")

    def __init__(self, cells: Sequence[ResolvedCell]) -> None:
        self.capacity: Dict[CellKey, int] = {c.key: c.capacity for c in cells}
        self.remaining: Dict[CellKey, int] = {c.key: c.capacity for c in cells}
        self.pending: Dict[CellKey, int] = {c.key: 0 for c in cells}
        self.paid: Dict[CellKey, int] = {c.key: 0 for c in cells}
        self.total = sum(self.capacity.values())
        self._remaining_total = self.total
        self._pending_total = 0
        self._paid_total = 0

    # -- totals -------------------------------------------------------------------------
    @property
    def remaining_total(self) -> int:
        """Tickets still on sale (all cells)."""
        return self._remaining_total

    @property
    def pending_total(self) -> int:
        """Tickets held by open (PENDING) orders."""
        return self._pending_total

    @property
    def paid_total(self) -> int:
        """Tickets of paid orders."""
        return self._paid_total

    # -- mutations ----------------------------------------------------------------------
    def take(self, cell: CellKey, qty: int) -> bool:
        """Buy ``qty`` tickets of one cell; all-or-nothing (no partial fills)."""
        if qty <= 0:
            return False
        available = self.remaining.get(cell)
        if available is None or available < qty:
            return False
        self.remaining[cell] = available - qty
        self.pending[cell] += qty
        self._remaining_total -= qty
        self._pending_total += qty
        return True

    def checkout(self, cell: CellKey, qty: int) -> None:
        """A pending order was paid for: the seats stay with the buyer."""
        self.pending[cell] -= qty
        self.paid[cell] += qty
        self._pending_total -= qty
        self._paid_total += qty

    def release(self, cell: CellKey, qty: int, from_paid: bool) -> None:
        """Seats return to the cell they came from (refund or platform clearing)."""
        if from_paid:
            self.paid[cell] -= qty
            self._paid_total -= qty
        else:
            self.pending[cell] -= qty
            self._pending_total -= qty
        self.remaining[cell] += qty
        self._remaining_total += qty

    # -- queries ------------------------------------------------------------------------
    def cell_remaining(self, cell: CellKey) -> int:
        return self.remaining.get(cell, 0)

    def any_available(self) -> bool:
        """True when at least one cell still has a ticket."""
        return self._remaining_total > 0

    def stocked_cells(self) -> Iterable[CellKey]:
        """The cells that still have tickets, in cell order."""
        for key, left in self.remaining.items():
            if left > 0:
                yield key

    def check(self) -> bool:
        """True when the conservation invariant holds."""
        if self._remaining_total < 0 or self._pending_total < 0 or self._paid_total < 0:
            return False
        if sum(self.capacity.values()) != self.total:
            return False
        if sum(self.remaining.values()) != self._remaining_total:
            return False
        if sum(self.pending.values()) != self._pending_total:
            return False
        if sum(self.paid.values()) != self._paid_total:
            return False
        return self.total == self._remaining_total + self._pending_total + self._paid_total

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"CellPool(cells={len(self.capacity)}, total={self.total}, "
                f"remaining={self._remaining_total}, pending={self._pending_total}, "
                f"paid={self._paid_total})")
