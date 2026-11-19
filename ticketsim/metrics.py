"""The counters of a run.

Port boundary: this is the **Python reference**; the Rust world core has the
same counters in ``core/ext/src/engine.rs`` (``Metrics``, same field names;
the Rust engine returns them from ``Core.metrics()`` and builds this class).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Metrics:
    """Counters collected while the simulation runs."""

    # -- the ticket pool -----------------------------------------------------------------
    sold_out_at: Dict[str, float] = field(default_factory=dict)
    refunded_tickets: int = 0          # returned at the decision time (straight back on sale)
    cleared_tickets: int = 0           # returned in a batch (orders that were never paid)
    tickets_sold: int = 0              # every ticket ever granted, resales included

    # -- buyers --------------------------------------------------------------------------
    buyer_attempts: int = 0
    buyer_successes: int = 0
    buyer_failures: int = 0
    buyer_stale_failures: int = 0      # the cell held less than the buyer had seen
    buyer_cell_short_failures: int = 0  # the cell could not serve the request
    buyer_retries: int = 0
    buyer_orders: int = 0
    buyer_parked: int = 0      # how often a buyer had to wait for tickets to come back
    buyer_wakes: int = 0        # how many waiting buyers were woken up
    buyer_capped: int = 0       # buyers that stopped because of the purchase limit
    buyer_fingerprint: int = 0    # rolling digest of the buyer draws (reproducibility check)

    # -- platform load -------------------------------------------------------------------
    #: every look at the inventory page; submits are ``buyer_attempts`` and the
    #: polls are derived from them (see ``summary.traffic_summary``)
    buyer_inventory_reads: int = 0

    # -- runtime self checks -------------------------------------------------------------
    #: number of times a rule was violated (must stay 0): a buyer asked for
    #: more tickets than it had seen.
    rule_violations: int = 0

    @property
    def failure_rate(self) -> float:
        return self.buyer_failures / self.buyer_attempts if self.buyer_attempts else 0.0
