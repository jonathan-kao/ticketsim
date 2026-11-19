"""The engine backed by the Rust world core (docs/ENGINEERING.md).

The world (buyers, orders, settlement, batches of returned seats) runs in
``ticketsim_core`` (the extension built from ``core/``).  The configuration,
the summary and every interface stay in Python.

The seam is ``advance()``: the core runs events and appends traced events to
``self.trace`` (and hands them to ``self.observers``); it keeps no reference
to this object between two calls.  The cost of a run is entirely in the core.

``RustEngine`` has the interface of ``engine.PythonEngine`` and reproduces it
bit for bit (``tests/test_backends.py``, ``tests/golden``).

Reading the world from Python
    ``sessions``, ``metrics``, ``pool_history`` and ``_bought`` are
    **snapshots**: every access returns the state at that moment.  They cannot
    be read from inside an observer - the core is in the middle of an event
    then; ``now`` can.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .crowd import CROWDS
from .engine import RunResult
from .metrics import Metrics
from .order import Order, OrderState
from .schema import CellKey, Config, ResolvedCell, SessionSpec, resolve_cells
from .summary import run_summary, session_summary, traffic_summary

try:
    from . import ticketsim_core
except ImportError as exc:                                  # pragma: no cover - build issue
    raise ImportError(
        "the Rust world core (ticketsim/ticketsim_core) is not built.\n"
        "  build it:           mise run build    (or: python3 scripts/build_core.py)\n"
        "  or run without it:  TICKETSIM_BACKEND=python  (the Python reference engine)"
    ) from exc


class PoolState:
    """The ticket pool of one session at one moment (the shape of ``pool.CellPool``)."""

    def __init__(self, state: Dict[str, Any]) -> None:
        self.capacity: Dict[CellKey, int] = state["capacity"]
        self.remaining: Dict[CellKey, int] = state["remaining"]
        self.pending: Dict[CellKey, int] = state["pending"]
        self.paid: Dict[CellKey, int] = state["paid"]
        self.total: int = state["total"]
        self.remaining_total: int = state["remaining_total"]
        self.pending_total: int = state["pending_total"]
        self.paid_total: int = state["paid_total"]
        self._ok: bool = state["pool_ok"]

    def cell_remaining(self, cell: CellKey) -> int:
        return self.remaining.get(cell, 0)

    def any_available(self) -> bool:
        return self.remaining_total > 0

    def check(self) -> bool:
        """True when the conservation invariant holds (checked by the core)."""
        return self._ok


class SessionState:
    """One session at one moment (the shape of ``session.Session``)."""

    def __init__(self, core: Any, spec: SessionSpec, cells: Tuple[ResolvedCell, ...]) -> None:
        self._core = core
        self.spec = spec
        self.cells = cells
        self.cell_by_key: Dict[CellKey, ResolvedCell] = {c.key: c for c in cells}
        state = core.session(spec.key)
        self.pool = PoolState(state)
        self.limbo: Dict[int, Dict[CellKey, int]] = state["limbo"]
        self.sold_out_at: Optional[float] = state["sold_out_at"]
        self.buyer_attempts: int = state["buyer_attempts"]
        self.buyer_successes: int = state["buyer_successes"]
        self.buyer_failures: int = state["buyer_failures"]
        self.stale_failures: int = state["stale_failures"]
        self.cell_short_failures: int = state["cell_short_failures"]
        self.refunded_tickets: int = state["refunded_tickets"]
        self.cleared_tickets: int = state["cleared_tickets"]
        self._held: int = state["held"]
        self._orders: Optional[Dict[int, Order]] = None

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def orders(self) -> Dict[int, Order]:
        """Every order placed in this session, by id (built on first use)."""
        if self._orders is None:
            self._orders = {
                row[0]: Order(id=row[0], session=self.spec.key, qty=row[1], placed_at=row[2],
                              state=OrderState(row[3]), settle_at=row[4], key=row[5],
                              seats=row[6], requests=row[7], buyer_index=row[8])
                for row in self._core.orders(self.spec.key)
            }
        return self._orders

    def is_open(self, now: float) -> bool:
        return now >= self.spec.open_at and (not self.spec.close_at or now < self.spec.close_at)

    def live_orders(self) -> Iterator[Order]:
        """Every order that still exists (PENDING and PAID)."""
        return (order for order in self.orders.values() if order.live)

    def held_tickets(self) -> int:
        """Tickets of orders that still exist (pending or paid)."""
        return self._held

    def observable(self, cell: CellKey) -> int:
        """What the inventory page shows for one cell."""
        return min(self.pool.cell_remaining(cell), self.spec.per_buyer_limit)


class RustEngine:
    """Runs one scenario on the Rust world core."""

    backend = "rust"

    def __init__(self, config: Config, trace_enabled: bool = True, trace_limit: int = 50000,
                 order_cap: Optional[int] = None,
                 crowd_cache: Optional[Dict[Any, Any]] = None,
                 trace_buyers: bool = False, trace_exact: bool = False) -> None:
        config.validate()
        self.config = config
        self.order_cap = order_cap
        #: crowds are reused between runs that share the seed and the crowd parameters
        self._crowd_cache = crowd_cache if crowd_cache is not None else {}
        self.seed = config.model_constants.seed
        self.trace_enabled = trace_enabled
        self.trace_limit = trace_limit
        self.trace_buyers = trace_buyers
        self.trace_exact = trace_exact
        #: the traced events (the core appends to this very list)
        self.trace: List[Dict[str, Any]] = []
        #: observers see every traced event as it happens (the web UI uses this)
        self.observers: List[Callable[[Dict[str, Any]], None]] = []

        self._started = False
        self._result: Optional[RunResult] = None
        self._cells: Dict[str, Tuple[ResolvedCell, ...]] = {
            spec.key: resolve_cells(spec, config.model_constants, self.seed)
            for spec in config.scenario.sessions
        }
        self._core = ticketsim_core.Core(self._core_params())
        self._clock = self._core.clock

    # ----------------------------------------------------------------------------------
    # the parameters of the core
    # ----------------------------------------------------------------------------------
    def _core_params(self) -> Dict[str, Any]:
        """Everything the core needs, as plain values (the cells already resolved)."""
        config = self.config
        scenario, behavior, mc = config.scenario, config.behavior, config.model_constants
        buyer_mix = tuple((str(m[0]), float(m[1]), float(m[2]), float(m[3]), float(m[4]))
                          for m in mc.buyer_mix)
        qty_weights = tuple(float(w) for w in mc.buyer_qty_weights)
        # the resolved stock, not ``spec.seats`` (a hand-written plan may leave it at 0)
        total_seats = sum(cell.capacity for cells in self._cells.values() for cell in cells)
        sessions = []
        for spec in scenario.sessions:
            cells = self._cells[spec.key]
            # the crowd is shared out in proportion to the stock
            if total_seats <= 0:
                buyers = 0
            else:
                share = sum(cell.capacity for cell in cells) / total_seats
                buyers = int(round(scenario.buyers * share))
            if self.order_cap is not None:
                buyers = min(buyers, self.order_cap)
            sessions.append({
                "key": spec.key,
                "per_buyer_limit": spec.per_buyer_limit,
                "open_at": spec.open_at,
                "close_at": spec.close_at,
                "buyers": buyers,
                "cells": [(cell.key[0], cell.key[1], cell.label, cell.capacity, cell.heat)
                          for cell in cells],
                # a crowd is a function of exactly these values (a smaller key
                # would reuse the buyers of another world)
                "crowd_key": ("rust", spec.key, buyers, self.seed, spec.per_buyer_limit,
                              buyer_mix, qty_weights, mc.count_jitter,
                              behavior.hot_buyer_frac),
            })
        settle = behavior.buyer_settle
        return {
            "seed": str(self.seed),
            "horizon": scenario.horizon,
            "sessions": sessions,
            "refund_prob": behavior.refund_prob,
            "clear_prob": behavior.clear_prob,
            "clear_period": behavior.clear_period,
            "payment_window": behavior.payment_window,
            "wake_budget_per_ticket": behavior.wake_budget_per_ticket,
            "hot_buyer_frac": behavior.hot_buyer_frac,
            "buyer_settle_kind": settle.kind,
            "buyer_settle_early_frac": settle.early_frac,
            "buyer_settle_early_range": (settle.early_range[0], settle.early_range[1]),
            "buyer_settle_late_range": (settle.late_range[0], settle.late_range[1]),
            "poll_seconds": mc.poll_seconds,
            "buyer_mix": buyer_mix,
            "buyer_qty_weights": qty_weights,
            "count_jitter": mc.count_jitter,
            "trace_enabled": self.trace_enabled,
            "trace_limit": self.trace_limit,
            "trace_buyers": self.trace_buyers,
            "trace_exact": self.trace_exact,
        }

    # ----------------------------------------------------------------------------------
    # running
    # ----------------------------------------------------------------------------------
    def run(self) -> RunResult:
        """Run the whole scenario and return its result."""
        while not self.advance(1_000_000):
            pass
        assert self._result is not None
        return self._result

    def start(self) -> None:
        """Push the opening events (called by :meth:`advance` on the first call)."""
        if self._started:
            return
        impl = self.config.model_constants.crowd_impl
        if impl not in CROWDS:
            raise KeyError(f"unknown crowd implementation {impl!r}; available: {sorted(CROWDS)}")
        self._core.start()
        self._started = True

    def advance(self, max_events: int = 1000) -> bool:
        """Process at most ``max_events`` events; return ``True`` when the run is over.

        The live UI drives the simulation through this method, so a run can be
        paused, stepped or fast-forwarded without changing the event order.
        """
        if not self._started:
            self.start()
        done = self._core.advance(self, max_events)
        if done and self._result is None:
            self._result = self._finalise()
        return done

    def _finalise(self) -> RunResult:
        metrics = Metrics(**self._core.metrics())
        sessions = self.sessions
        traffic = traffic_summary(metrics, self.config)
        summary = run_summary(metrics, sessions, traffic, self.order_cap)
        return RunResult(
            config=self.config, metrics=metrics, summary=summary, trace=self.trace,
            sessions={key: session_summary(session) for key, session in sessions.items()},
            pool_history=self._core.pool_history())

    # ----------------------------------------------------------------------------------
    # the world, as seen from Python (snapshots)
    # ----------------------------------------------------------------------------------
    @property
    def now(self) -> float:
        """The simulation clock (safe to read from an observer)."""
        return self._clock.now

    @property
    def events_processed(self) -> int:
        return self._core.events_processed

    @property
    def metrics(self) -> Metrics:
        """The counters so far; the final ones once the run is over."""
        if self._result is not None:
            return self._result.metrics
        return Metrics(**self._core.metrics())

    @property
    def sessions(self) -> Dict[str, SessionState]:
        return {spec.key: SessionState(self._core, spec, self._cells[spec.key])
                for spec in self.config.scenario.sessions}

    @property
    def pool_history(self) -> List[Tuple[float, str, int]]:
        """``(time, session, tickets on sale)`` after every change of a pool."""
        if self._result is not None:
            return self._result.pool_history
        return self._core.pool_history()

    @property
    def _bought(self) -> Dict[str, List[int]]:
        """Tickets bought by every buyer (buyer index → tickets) of the sessions on sale."""
        bought = ((spec.key, self._core.buyer_bought(spec.key))
                  for spec in self.config.scenario.sessions)
        return {key: tickets for key, tickets in bought if tickets is not None}
