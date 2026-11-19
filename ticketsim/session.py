"""A session: its seating plan, its ticket pool, its orders and its sale window.

The stock of a session lives in **cells** (one per area of the seating
plan).  A request can only take tickets from one
cell; an order may be made of several requests.

Port boundary: this is the **Python reference**; the Rust world core has the
same thing in ``core/world/src/session.rs`` (the order of ``alive``, the stable sort of
``hot_order`` and ``hot_ptr`` must match).
The two must stay bit-identical (``tests/test_backends.py`` compares them
event by event), so a change to the mechanism goes into both.
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .order import Order
from .pool import CellPool
from .rng import hash_uniform_bytes
from .schema import CellKey, ResolvedCell, SessionSpec


class Session:
    """One event on sale."""

    def __init__(self, spec: SessionSpec, cells: Sequence[ResolvedCell]) -> None:
        self.spec = spec
        self.cells: Tuple[ResolvedCell, ...] = tuple(cells)
        self.cell_by_key: Dict[CellKey, ResolvedCell] = {c.key: c for c in self.cells}
        #: cells ordered by popularity (hottest first) - the "best seats" order
        self.hot_order: Tuple[CellKey, ...] = tuple(
            c.key for c in sorted(self.cells,
                                  key=lambda c: (-c.heat, c.capacity, c.key[0])))
        self.pool = CellPool(self.cells)
        #: orders whose payment window ran out: the order is cancelled, but its
        #: seats only go back on sale with the next batch (engine._on_clear)
        self.limbo: Dict[int, Dict[CellKey, int]] = {}
        #: the cells that still have tickets (kept in cell order)
        self._alive: List[CellKey] = [c.key for c in self.cells if c.capacity > 0]
        self._alive_set: Set[CellKey] = set(self._alive)
        #: how far we have walked down ``hot_order`` (the hottest cells sell out first)
        self._hot_ptr = 0
        self.orders: Dict[int, Order] = {}
        self.sold_out_at: Optional[float] = None
        # statistics
        self.buyer_attempts = 0
        self.buyer_successes = 0
        self.buyer_failures = 0
        self.stale_failures = 0
        self.cell_short_failures = 0
        self.refunded_tickets = 0
        self.cleared_tickets = 0

    @property
    def key(self) -> str:
        return self.spec.key

    def is_open(self, now: float) -> bool:
        return now >= self.spec.open_at and (not self.spec.close_at or now < self.spec.close_at)

    def add_order(self, order: Order) -> None:
        self.orders[order.id] = order

    def live_orders(self) -> Iterator[Order]:
        """Every order that still exists (PENDING and PAID)."""
        for order in self.orders.values():
            if order.live:
                yield order

    def held_tickets(self) -> int:
        """Tickets of orders that still exist (pending or paid)."""
        return sum(o.qty for o in self.live_orders())

    # -- what the inventory page shows -------------------------------------------------
    def observable(self, cell: CellKey) -> int:
        """What the inventory page shows for one cell.

        The page prints the exact number while the cell is small; a cell with
        more than the purchase limit only says "available", so the best a buyer
        can read is the limit itself.  That is enough: a single request never
        asks for more than the buyer's quota.
        """
        return min(self.pool.cell_remaining(cell), self.spec.per_buyer_limit)

    # -- stock movements -----------------------------------------------------------------
    def sell(self, cell: CellKey, qty: int) -> bool:
        """Take ``qty`` tickets of one cell (all or nothing)."""
        if not self.pool.take(cell, qty):
            return False
        if self.pool.cell_remaining(cell) <= 0 and cell in self._alive_set:
            self._alive_set.discard(cell)
            self._alive.remove(cell)
        return True

    def return_seats(self, cell: CellKey, qty: int, from_paid: bool) -> None:
        """Give the seats back to the cell they came from."""
        self.pool.release(cell, qty, from_paid=from_paid)
        if cell not in self._alive_set and self.pool.cell_remaining(cell) > 0:
            self._alive_set.add(cell)
            self._alive.append(cell)
            index = self.hot_order.index(cell) if cell in self.hot_order else None
            if index is not None and index < self._hot_ptr:
                self._hot_ptr = index      # a hot cell is available again

    # -- picking a cell ----------------------------------------------------------------
    def pick_hot_cell(self) -> Optional[CellKey]:
        """The most popular cell that still has tickets."""
        while self._hot_ptr < len(self.hot_order):
            key = self.hot_order[self._hot_ptr]
            if self.pool.cell_remaining(key) > 0:
                return key
            self._hot_ptr += 1
        return None

    def pick_random_cell(self, seed: int, buyer: int, attempt: int) -> Optional[CellKey]:
        """A uniformly random cell that still has tickets.

        The draw is keyed by the buyer and the attempt, so it does not depend
        on the order in which buyers act (common random numbers).
        """
        alive = self._alive
        if not alive:
            return None
        u = hash_uniform_bytes(
            f"{seed}:b:{self.key}:{buyer}:cellpick:{attempt}".encode("utf-8"))
        return alive[int(u * len(alive))]
