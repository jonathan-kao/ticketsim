"""Order state machine.

    PENDING ──(decision: refund)──▶ CLOSED   seats back in the pool at once
            ──(decision: pay)─────▶ PAID     seats sold for good
            ──(decision: unpaid)──▶ PENDING until the payment window ends, then
                                    CLOSED; the seats wait for the next batch

Port boundary: this is the **Python reference**; the Rust world core has the
same thing in ``core/world/src/session.rs`` (``Order`` / ``OrderState``).  The
two must stay bit-identical (``tests/test_backends.py`` compares them event by
event), so a change to the mechanism goes into both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from .schema import CellKey


class OrderState(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CLOSED = "CLOSED"


@dataclass
class Order:
    """One order placed in a session.

    An order may be made of several requests (a buyer who did not get every
    ticket at once tops the order up): ``seats`` keeps the number of tickets
    per cell, ``qty`` is the total.
    """

    id: int
    session: str
    qty: int
    placed_at: float
    state: OrderState = OrderState.PENDING
    settle_at: Optional[float] = None
    key: str = ""                          # stable identity for order independent randomness
    seats: Dict[CellKey, int] = field(default_factory=dict)
    requests: int = 0                      # how many requests this order is made of
    buyer_index: Optional[int] = None      # the buyer behind this order

    @property
    def live(self) -> bool:
        """Still exists (PENDING or PAID)."""
        return self.state is not OrderState.CLOSED
