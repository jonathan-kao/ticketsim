"""The discrete event engine.

Two interchangeable implementations of the same model (docs/ENGINEERING.md):

* ``RustEngine`` (``rust_engine.py`` + the ``ticketsim_core`` extension,
  ``core/``) - the world core in Rust; **the default**.
* ``PythonEngine`` (this module) - the readable **reference implementation**.
  The Rust core is a bit for bit port of it: ``tests/test_backends.py``
  compares the two event by event, and both must reproduce
  ``tests/golden/*.json``.

``Engine(config, ...)`` builds the one selected by ``backend=`` (or the
``TICKETSIM_BACKEND`` environment variable).

A ``heapq`` of ``(time, priority, sequence)`` drives the world.  Event order
inside one timestamp is fixed by the priority constants below, so a run is
reproducible bit for bit.

    * sessions open; the whole crowd arrives at the opening;
    * a request takes tickets from **one cell** of the seating plan;
    * an order may be made of several requests (a buyer tops it up);
    * every buyer is limited by the per-session purchase limit;
    * orders are paid, cancelled (seats back at once) or never paid (seats
      back with the next batch).
"""
from __future__ import annotations

import os
from array import array
from heapq import heappop, heappush
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .crowd import Crowd, make_crowd
from .metrics import Metrics
from .order import Order, OrderState
from .rng import Channels, hash_uniform_bytes, stream_uniform
from .schema import CellKey, Config, resolve_cells
from .session import Session
from .summary import run_summary, session_summary, traffic_summary

# Event priorities (lower runs first inside one timestamp).  The gaps are the
# priorities a model with more event kinds would use; keeping the numbers
# keeps the order of the remaining events identical.
PRIO_SALE_OPEN = 0
PRIO_BUYER_WAVE = 1
PRIO_CLEAR = 2
PRIO_BUYER_SUBMIT = 5      # the engine no longer needs this event (see _buyer_plan);
                           # kept for the reference model in tests/test_equivalence.py
PRIO_BUYER_GRANT = 7       # the platform answers a request
PRIO_SETTLE = 8
PRIO_BUYER_OBSERVE = 9


@dataclass
class RunResult:
    """Everything one run produced."""

    config: Config
    metrics: Metrics
    summary: Dict[str, Any]
    trace: List[Dict[str, Any]] = field(default_factory=list)
    sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pool_history: List[Tuple[float, str, int]] = field(default_factory=list)


class PythonEngine:
    """Runs one scenario - the pure Python reference implementation."""

    backend = "python"

    def __init__(self, config: Config, trace_enabled: bool = True, trace_limit: int = 50000,
                 order_cap: Optional[int] = None,
                 crowd_cache: Optional[Dict[Any, Any]] = None,
                 trace_buyers: bool = False, trace_exact: bool = False) -> None:
        config.validate()
        self.config = config
        self.order_cap = order_cap
        #: crowds are reused between runs that share the seed and the crowd
        #: parameters (a parameter scan changes the rules, not the people)
        self._crowd_cache = crowd_cache if crowd_cache is not None else {}
        self.seed = config.model_constants.seed
        self.channels = Channels(self.seed)
        self.metrics = Metrics()
        self.trace_enabled = trace_enabled
        self.trace_limit = trace_limit
        #: also trace the buyers' events (the golden fixtures need them:
        #: they are the specification of the ticket pool's dynamics)
        self.trace_buyers = trace_buyers
        #: keep the *unrounded* timestamp in the trace (the golden fixtures
        #: compare times bit by bit; the UI wants the readable one)
        self.trace_exact = trace_exact
        self.trace: List[Dict[str, Any]] = []
        #: observers see every traced event as it happens (the web UI uses this)
        self.observers: List[Callable[[Dict[str, Any]], None]] = []

        self.now = 0.0
        self._heap: List[Tuple[float, int, int, str, Any]] = []
        self._seq = 0
        self._order_seq = 0
        self._started = False
        self._finished = False
        self._result: Optional[RunResult] = None
        self.events_processed = 0

        self.sessions: Dict[str, Session] = {}
        for spec in config.scenario.sessions:
            cells = resolve_cells(spec, config.model_constants, config.model_constants.seed)
            self.sessions[spec.key] = Session(spec, cells)
        self._crowds: Dict[str, Crowd] = {}
        self._parked: Dict[str, List[int]] = {}      # buyer indices waiting for tickets
        self._attempts: Dict[str, array] = {}        # buyer index → attempts so far
        self._bought: Dict[str, array] = {}          # buyer index → tickets bought here
        self._buyer_orders: Dict[str, Dict[int, int]] = {}   # buyer index → open order id
        #: observations that were deferred to the end of their timestamp
        #: (see :meth:`_flush_picks` - the retry loop used to spend one
        #: event per attempt just to look at the inventory page again)
        self._pending_picks: List[Tuple[str, int]] = []
        self._pending_time = 0.0
        self.pool_history: List[Tuple[float, str, int]] = []
        self._handlers = {
            "sale_open": self._on_sale_open,
            "buyer_wave": self._on_buyer_wave,
            "buyer_observe": self._on_buyer_observe,
            "buyer_grant": self._on_buyer_grant,
            "clear": self._on_clear,
            "settle": self._on_settle,
            "expire": self._on_expire,
        }

    # ----------------------------------------------------------------------------------
    # event plumbing
    # ----------------------------------------------------------------------------------
    def _push(self, time: float, priority: int, payload: Tuple[str, Any]) -> None:
        self._seq += 1
        heappush(self._heap, (time, priority, self._seq, payload[0], payload[1]))

    def _trace(self, kind: str, **fields: Any) -> None:
        item = {"t": round(self.now, 4), "kind": kind, **fields}
        if self.trace_exact:
            item["t_exact"] = self.now
        for observer in self.observers:
            observer(item)
        if self.trace_enabled and len(self.trace) < self.trace_limit:
            self.trace.append(item)

    def run(self) -> RunResult:
        """Run the whole scenario and return its result.

        The world starts when the first session opens: nothing before the
        opening is modelled.
        """
        while not self.advance(10000):
            pass
        assert self._result is not None
        return self._result

    def start(self) -> None:
        """Push the opening events (called by :meth:`advance` on the first call)."""
        if self._started:
            return
        for session in self.sessions.values():
            self._push(session.spec.open_at, PRIO_SALE_OPEN, ("sale_open", session.key))
        self._started = True

    def advance(self, max_events: int = 1000) -> bool:
        """Process at most ``max_events`` events; return ``True`` when the run is over.

        The live UI drives the simulation through this method, so a run can be
        paused, stepped or fast-forwarded without changing the event order.
        """
        if not self._started:
            self.start()
        horizon = self.config.scenario.horizon
        processed = 0
        heap = self._heap
        pop = heappop
        handlers = self._handlers
        while processed < max_events:
            if self._pending_picks and (not heap or heap[0][0] > self._pending_time):
                # The deferred observations belong to the end of an earlier
                # moment, and the events they schedule may come before the next
                # one in the queue: run them before taking it.
                self._flush_picks()
                continue
            if not heap:
                break
            time, prio, seq, kind, payload = pop(heap)
            if time > horizon:
                self._finished = True
                break
            self.now = time
            handlers[kind](payload)
            processed += 1
        self.events_processed += processed
        if self._finished or not self._heap:
            if self._pending_picks:
                self._flush_picks()
            self.now = horizon
            self._result = self._finalise()
            return True
        return False

    def _flush_picks(self) -> None:
        """Run the observations that were deferred to the end of their timestamp.

        A buyer that lost looks at the inventory page again.  That observation
        used to be an event of its own, scheduled at the *end* of the current
        timestamp (priority 9, after every purchase of that instant).  Now it
        is deferred to exactly the same point of the timeline, which keeps the
        order and the pool state the buyer sees **identical** while saving one
        event per attempt - the retry loop is the biggest consumer of events.
        """
        pending = self._pending_picks
        if not pending:
            return
        self._pending_picks = []
        now = self.now
        self.now = self._pending_time
        for key, index in pending:
            self._buyer_plan(key, index)
        self.now = now

    # ----------------------------------------------------------------------------------
    # world
    # ----------------------------------------------------------------------------------
    def _on_sale_open(self, key: str) -> None:
        session = self.sessions[key]
        self._trace("sale_open", session=key)
        self._start_buyer_stream(session)
        self._push(session.spec.open_at, PRIO_BUYER_WAVE, ("buyer_wave", session.key))
        first_clear = session.spec.open_at + self.config.behavior.clear_period
        if first_clear <= self.config.scenario.horizon:
            self._push(first_clear, PRIO_CLEAR, ("clear", (key, 1)))

    def _start_buyer_stream(self, session: Session) -> None:
        """Buyers of one session: proportional to its stock, lazy arrivals.

        The stock is the resolved seating plan (``pool.total``), not the
        ``seats`` field: a hand-written plan may leave ``seats`` at 0.
        """
        total_seats = sum(s.pool.total for s in self.sessions.values())
        if total_seats <= 0:
            count = 0
        else:
            share = session.pool.total / total_seats
            count = int(round(self.config.scenario.buyers * share))
        if self.order_cap is not None:
            count = min(count, self.order_cap)
        impl = self.config.model_constants.crowd_impl
        cache_key = (impl, session.key, count, self.seed, session.spec.per_buyer_limit,
                     self.config.model_constants.buyer_mix, self.config.model_constants.buyer_qty_weights,
                     self.config.model_constants.count_jitter, self.config.behavior.hot_buyer_frac)
        crowd = self._crowd_cache.get(cache_key)
        if crowd is None:
            crowd = make_crowd(impl, session.key, count, self.config, self.channels,
                               max_qty=session.spec.per_buyer_limit)
            self._crowd_cache[cache_key] = crowd
        self._crowds[session.key] = crowd
        self._parked[session.key] = []
        self._attempts[session.key] = array("i", bytes(4 * len(crowd)))
        self._bought[session.key] = array("i", bytes(4 * len(crowd)))
        self._buyer_orders[session.key] = {}

    def _on_buyer_wave(self, key: str) -> None:
        """The whole crowd reads the inventory page at the moment the sale opens.

        The observation, the request and the platform's answer are collapsed
        into **one** event: the answer is scheduled directly (the buyer's own
        delay plus the polling latency).  The numbers are the same as with the
        three event chain (observe → submit → grant); only the bookkeeping
        events are gone.
        """
        crowd = self._crowds.get(key)
        if crowd is None:
            return
        session = self.sessions[key]
        open_at = session.spec.open_at
        poll = self.config.model_constants.poll_seconds
        attempts = self._attempts[key]
        metrics = self.metrics
        heap = self._heap
        seq = self._seq
        for index in range(len(crowd)):
            self._fingerprint_buyer(key, index)
            metrics.buyer_inventory_reads += 1
            status, cell, qty, observed = self._buyer_request(key, index)
            if status != "ok":
                if status == "empty":
                    self._park(key, index)
                    if self.trace_buyers:
                        self._trace("buyer_park", session=key, buyer=index)
                continue
            delay = crowd.first_delay(index)
            attempts[index] += 1
            session.buyer_attempts += 1
            metrics.buyer_attempts += 1
            if qty > observed:
                metrics.rule_violations += 1
            if self.trace_buyers:
                self._trace("buyer_req", session=key, buyer=index, cell=cell,
                            qty=qty, observed=observed, delay=delay)
            seq += 1
            heappush(heap, (open_at + delay + poll, PRIO_BUYER_GRANT, seq,
                            "buyer_grant", (key, index, cell, qty, observed)))
        if self.trace_buyers:
            self._trace("buyer_wave", session=key, buyers=len(crowd))
        self._seq = seq

    def _on_buyer_observe(self, payload: Tuple[str, int]) -> None:
        """A buyer looks at the inventory page again (after a failed order or a top-up)."""
        key, index = payload
        self._buyer_plan(key, index)

    def _buyer_plan(self, key: str, index: int) -> None:
        """Observe the inventory page and schedule the platform's answer.

        One attempt = one event: the buyer observes now, presses the button
        after its own delay, and the platform answers ``poll_seconds`` later.
        """
        self.metrics.buyer_inventory_reads += 1
        status, cell, qty, observed = self._buyer_request(key, index)
        if status != "ok":
            if status == "empty":
                self._park(key, index)
                if self.trace_buyers:
                    self._trace("buyer_park", session=key, buyer=index)
            return
        attempts = self._attempts[key]
        count = attempts[index]
        if count == 0:
            delay = self._crowds[key].first_delay(index)
        else:
            delay = self._crowds[key].next_delay(index, count - 1)
        attempts[index] = count + 1
        session = self.sessions[key]
        session.buyer_attempts += 1
        metrics = self.metrics
        metrics.buyer_attempts += 1
        if qty > observed:
            metrics.rule_violations += 1
        if self.trace_buyers:
            self._trace("buyer_req", session=key, buyer=index, cell=cell,
                        qty=qty, observed=observed, delay=delay)
        self._seq += 1
        heappush(self._heap, (self.now + delay + self.config.model_constants.poll_seconds,
                              PRIO_BUYER_GRANT, self._seq, "buyer_grant",
                              (key, index, cell, qty, observed)))

    def _buyer_request(self, key: str, index: int) -> Tuple[str, Optional[CellKey], int, int]:
        """What a buyer wants to buy right now: (status, cell, qty, observed).

        ``status`` is ``ok`` (a request is possible), ``done`` (the sale is
        closed, or the buyer has what it wanted or hit the purchase limit) or
        ``empty`` (nothing to buy right now → wait for the tickets to come back).

        This is the hottest function of the whole simulation (once per
        attempt), so the cell choice and the observation are inlined.
        """
        session = self.sessions[key]
        close_at = session.spec.close_at
        if close_at and self.now >= close_at:
            return "done", None, 0, 0                   # the sale window is over
        crowd = self._crowds[key]
        bought = self._bought[key][index]
        quota = session.spec.per_buyer_limit - bought
        if quota <= 0:
            self.metrics.buyer_capped += 1              # hit the purchase limit, stop
            return "done", None, 0, 0
        desired = crowd.desired(index) - bought
        if desired <= 0:
            return "done", None, 0, 0
        if session.pool.remaining_total == 0:
            # nothing left at all → the cell choice would fail anyway
            return "empty", None, 0, 0
        # The hot buyers take the most popular seats, the others pick at random.
        if crowd.hot(index):
            cell = session.pick_hot_cell()
        else:
            cell = session.pick_random_cell(self.seed, index + 1, self._attempts[key][index])
        if cell is None:
            return "empty", None, 0, 0
        observed = session.observable(cell)
        qty = desired if desired < quota else quota
        if observed < qty:
            qty = observed
        if qty <= 0:
            return "empty", None, 0, 0
        return "ok", cell, qty, observed

    def _park(self, key: str, index: int) -> None:
        """A buyer that cannot buy waits until the session has tickets again."""
        self._parked[key].append(index)
        self.metrics.buyer_parked += 1

    def _maybe_wake(self, session: Session, released: int = 1) -> None:
        """Wake some of the waiting buyers when tickets come back.

        Waking **everybody** would turn every single returned ticket into a
        stampede of tens of thousands of requests (and the event count with
        it).  Real crowds poll at their own rate, so only a few contenders per
        returned ticket are woken (``wake_budget_per_ticket``).
        """
        parked = self._parked.get(session.key)
        if not parked:
            return
        if not session.pool.any_available():
            return                       # still nothing anybody could buy
        budget = max(1, int(released * self.config.behavior.wake_budget_per_ticket))
        count = min(budget, len(parked))
        waiting = parked[:count]
        del parked[:count]
        for index in waiting:
            self._push(self.now, PRIO_BUYER_OBSERVE, ("buyer_observe", (session.key, index)))
        self.metrics.buyer_wakes += count
        if self.trace_buyers:
            self._trace("buyer_wake", session=session.key, count=count, released=released)

    def _buyer_delay(self, key: str, index: int) -> float:
        """The buyer's own speed - also the interval between two attempts."""
        crowd = self._crowds[key]
        retries = max(0, self._attempts[key][index] - 1)
        return crowd.next_delay(index, retries)

    def _fingerprint_buyer(self, key: str, index: int) -> None:
        """A cheap rolling digest of the buyer draws.

        The digest only depends on the crowd's draws: two runs that should see
        the same buyers (same seed, same crowd) must produce the same digest.
        """
        crowd = self._crowds[key]
        self.metrics.buyer_fingerprint = (
            self.metrics.buyer_fingerprint * 1000003
            + (index + 1) * 7919
            + crowd.desired(index) * 17
        ) % (2 ** 61)

    def _record_pool(self, session: Session) -> None:
        """Keep the pool trajectory (used by the visualisation and by tests)."""
        self.pool_history.append((round(self.now, 4), session.key, session.pool.remaining_total))

    def _on_buyer_grant(self, payload: Tuple[str, int, CellKey, int, int]) -> None:
        """The platform grants (or refuses) the buyer's ticket."""
        key, index, cell, qty, observed = payload
        session = self.sessions[key]
        left = session.pool.cell_remaining(cell)
        ok = session.sell(cell, qty)
        if self.trace_buyers:
            self._trace("buyer_grant", session=key, buyer=index, cell=cell, qty=qty,
                        observed=observed, left=left, ok=ok)
        if ok:
            session.buyer_successes += 1
            self.metrics.buyer_successes += 1
            self.metrics.buyer_orders += 1
            self.metrics.tickets_sold += qty
            self._bought[key][index] += qty
            order = self._buyer_order(key, index)
            if order is None:
                order = self._new_order(session, qty, key=f"b:{key}:{index + 1}",
                                        buyer_index=index)
                order.seats[cell] = qty
                order.requests = 1
                self._buyer_orders[key][index] = order.id
                self._schedule_settle(order)
            else:
                order.seats[cell] = order.seats.get(cell, 0) + qty
                order.qty += qty
                order.requests += 1
            self._record_pool(session)
            # the buyer tops the order up when it is short of what it wanted
            if self._buyer_wants_more(key, index):
                self._push(self.now + self._buyer_delay(key, index), PRIO_BUYER_OBSERVE,
                           ("buyer_observe", (key, index)))
        else:
            session.buyer_failures += 1
            self.metrics.buyer_failures += 1
            if left < qty:
                session.cell_short_failures += 1
                self.metrics.buyer_cell_short_failures += 1
            if observed > left:
                session.stale_failures += 1
                self.metrics.buyer_stale_failures += 1
            self._maybe_retry(key, index)

    def _buyer_wants_more(self, key: str, index: int) -> bool:
        """The buyer still wants tickets and still has quota left."""
        session = self.sessions[key]
        bought = self._bought[key][index]
        return (bought < self._crowds[key].desired(index)
                and bought < session.spec.per_buyer_limit)

    def _buyer_order(self, key: str, index: int) -> Optional[Order]:
        """The buyer's open order in this session, if it has one."""
        order_id = self._buyer_orders.get(key, {}).get(index)
        if order_id is None:
            return None
        order = self.sessions[key].orders.get(order_id)
        if order is None or order.state is not OrderState.PENDING:
            return None
        return order

    def _maybe_retry(self, key: str, index: int) -> None:
        """A buyer that lost keeps trying; the interval is its own speed.

        The observation is deferred to the end of the current timestamp - the
        same point of the timeline the old ``buyer_observe`` event used to
        run at, without paying for an event (see :meth:`_flush_picks`).
        """
        self.metrics.buyer_retries += 1
        if not self._pending_picks:
            self._pending_time = self.now
        self._pending_picks.append((key, index))

    # ----------------------------------------------------------------------------------
    # orders
    # ----------------------------------------------------------------------------------
    def _new_order(self, session: Session, qty: int, key: str,
                   buyer_index: Optional[int] = None) -> Order:
        self._order_seq += 1
        order = Order(id=self._order_seq, session=session.key, qty=qty, placed_at=self.now,
                      key=key, buyer_index=buyer_index)
        session.add_order(order)
        if session.pool.remaining_total == 0 and session.sold_out_at is None:
            session.sold_out_at = self.now
            self.metrics.sold_out_at[session.key] = self.now
        return order

    def _schedule_settle(self, order: Order) -> None:
        order.settle_at = self.now + self._settle_delay(order)
        self._push(order.settle_at, PRIO_SETTLE, ("settle", order))

    def _settle_delay(self, order: Order) -> float:
        """When the buyer decides what to do with the order (its own time law)."""
        prefix = f"{self.seed}:order:{order.key or order.id}:settle".encode("utf-8")
        return self._buyer_settle_time(prefix)

    def _buyer_settle_time(self, prefix: bytes) -> float:
        spec = self.config.behavior.buyer_settle
        if spec.kind == "uniform":
            low, high = spec.early_range[0], spec.late_range[1]
            return low + (high - low) * stream_uniform(prefix, 0)
        if stream_uniform(prefix, 0) < spec.early_frac:
            low, high = spec.early_range
        else:
            low, high = spec.late_range
        return low + (high - low) * stream_uniform(prefix, 1)

    def _release_order(self, order: Order, from_paid: bool) -> None:
        """Give the seats of an order back to the cells they came from."""
        session = self.sessions[order.session]
        for cell, qty in order.seats.items():
            session.return_seats(cell, qty, from_paid=from_paid)

    def _forget_buyer_order(self, session: Session, order: Order) -> None:
        """The order is no longer open: the buyer's next purchase opens a new one."""
        if order.buyer_index is not None:
            self._buyer_orders.get(session.key, {}).pop(order.buyer_index, None)

    def _on_settle(self, order: Order) -> None:
        """The buyer's decision: cancel (refund), never pay, or pay.

        Each outcome is a keyed draw on the order, so it does not depend on
        what else happened in the run.
        """
        if order.state is not OrderState.PENDING:
            return
        session = self.sessions[order.session]
        key = order.key or str(order.id)
        refund = (hash_uniform_bytes(f"{self.seed}:order:{key}:refund".encode("utf-8"))
                  < self.config.behavior.refund_prob)
        unpaid = (not refund and
                  hash_uniform_bytes(f"{self.seed}:order:{key}:unpaid".encode("utf-8"))
                  < self.config.behavior.clear_prob)
        if refund:
            self._release_order(order, from_paid=False)
            order.state = OrderState.CLOSED
            session.refunded_tickets += order.qty
            self.metrics.refunded_tickets += order.qty
            self._record_pool(session)
            self._maybe_wake(session, order.qty)
            self._forget_buyer_order(session, order)
            self._trace("refund", session=order.session, qty=order.qty)
            return
        if unpaid:
            # never paid: the order is cancelled when its payment window ends and
            # the seats wait for the next batch
            self._schedule_expiry(order)
            self._trace("unpaid", session=order.session, qty=order.qty)
            return
        for cell, qty in order.seats.items():
            session.pool.checkout(cell, qty)
        order.state = OrderState.PAID
        self._forget_buyer_order(session, order)
        self._trace("checkout", session=order.session, qty=order.qty)

    def _schedule_expiry(self, order: Order) -> None:
        """The payment window of an unpaid order ends ``payment_window`` after it was placed."""
        window = self.config.behavior.payment_window
        self._push(order.placed_at + window, PRIO_SETTLE, ("expire", order))

    def _on_expire(self, order: Order) -> None:
        """The payment window ran out: the order is cancelled, its seats wait in limbo."""
        if order.state is not OrderState.PENDING:
            return
        session = self.sessions[order.session]
        order.state = OrderState.CLOSED
        session.limbo[order.id] = dict(order.seats)
        self._forget_buyer_order(session, order)
        self._trace("expired", session=order.session, qty=order.qty)

    def _on_clear(self, payload: Tuple[str, int]) -> None:
        """A batch: the seats of every expired order go back on sale together.

        An expired order is cancelled at once, but its seats are released in
        batches every ``clear_period`` seconds, so a large number of tickets
        comes back at the same moment.  Paid orders are never touched.
        """
        session_key, cycle = payload
        session = self.sessions[session_key]
        if session.limbo:
            released = 0
            for _order_id, seats in session.limbo.items():
                for cell, qty in seats.items():
                    session.return_seats(cell, qty, from_paid=False)
                    released += qty
            session.limbo.clear()
            session.cleared_tickets += released
            self.metrics.cleared_tickets += released
            self._record_pool(session)
            self._maybe_wake(session, released)
            self._trace("cleared", session=session.key, qty=released)
        next_clear = session.spec.open_at + (cycle + 1) * self.config.behavior.clear_period
        if next_clear <= self.config.scenario.horizon:
            self._push(next_clear, PRIO_CLEAR, ("clear", (session_key, cycle + 1)))

    # ----------------------------------------------------------------------------------
    # result
    # ----------------------------------------------------------------------------------
    def _finalise(self) -> RunResult:
        traffic = traffic_summary(self.metrics, self.config)
        summary = run_summary(self.metrics, self.sessions, traffic, self.order_cap)
        sessions = {key: session_summary(s) for key, s in self.sessions.items()}
        return RunResult(config=self.config, metrics=self.metrics, summary=summary,
                         trace=self.trace, sessions=sessions, pool_history=self.pool_history)


#: the engine implementations, by name
BACKENDS = ("rust", "python")

#: used when neither ``backend=`` nor ``TICKETSIM_BACKEND`` says otherwise
DEFAULT_BACKEND = "rust"


def backend_name(backend: Optional[str] = None) -> str:
    """The backend a run will use: the argument, else ``TICKETSIM_BACKEND``, else the default."""
    name = (backend or os.environ.get("TICKETSIM_BACKEND") or DEFAULT_BACKEND).lower()
    if name not in BACKENDS:
        raise ValueError(f"unknown engine backend {name!r}; available: {list(BACKENDS)}")
    return name


def Engine(config: Config,       # noqa: N802
           trace_enabled: bool = True, trace_limit: int = 50000,
           order_cap: Optional[int] = None,
           crowd_cache: Optional[Dict[Any, Any]] = None,
           trace_buyers: bool = False, trace_exact: bool = False,
           backend: Optional[str] = None):
    """Build the engine of one run.

    ``backend="rust"`` (the default) is the Rust world core; ``"python"`` is
    the reference implementation.  Both take the same arguments, expose the
    same interface and produce the same numbers, bit for bit.
    """
    if backend_name(backend) == "python":
        cls = PythonEngine
    else:
        from .rust_engine import RustEngine
        cls = RustEngine
    return cls(config, trace_enabled=trace_enabled, trace_limit=trace_limit,
               order_cap=order_cap, crowd_cache=crowd_cache,
               trace_buyers=trace_buyers, trace_exact=trace_exact)
