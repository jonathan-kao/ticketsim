"""Equivalence of the batched order flow and an explicit agent simulation.

The fast model (an aggregated order flow) must reproduce the slow model (one
object per buyer) exactly, for the same random draws.
The reference implementation below is deliberately written the naive way: every
buyer is an agent with its own state, all of them are materialised up front,
and the world is stepped by an explicit event queue.

The engine runs the *synthetic* crowd (parallel arrays); the reference uses the
*object* crowd and its own agents.  Both must agree event by event.

The world is made of **cells**: a request takes tickets from one cell and an
order may be made of several requests.  The reference mirrors the
engine's rules exactly (cell choice, cumulative purchase limit, top-ups,
waking budget).
"""
from __future__ import annotations

import heapq
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ticketsim.crowd import make_crowd
from ticketsim.engine import (PRIO_BUYER_GRANT, PRIO_BUYER_OBSERVE, PRIO_BUYER_SUBMIT,
                              PRIO_BUYER_WAVE, PRIO_CLEAR, PRIO_SALE_OPEN, PRIO_SETTLE)
from ticketsim.rng import Channels, hash_uniform
from ticketsim.schema import CellKey, Config, resolve_cells
from ticketsim.session import Session

from helpers import small_config


@dataclass
class Agent:
    """One explicit buyer."""

    index: int
    desired_qty: int
    attempts: int = 0
    bought: int = 0
    observed: int = 0
    qty: int = 0


@dataclass
class RefOrder:
    key: str
    session: str
    qty: int
    placed_at: float
    state: str = "PENDING"
    seats: Dict[CellKey, int] = field(default_factory=dict)
    requests: int = 0
    agent_index: Optional[int] = None


@dataclass
class RefWorld:
    """The naive, agent based world used as the reference."""

    config: Config
    sessions: Dict[str, Session] = field(default_factory=dict)
    orders: Dict[int, RefOrder] = field(default_factory=dict)   # keyed by a unique id
    history: List[Tuple[float, str, int]] = field(default_factory=list)
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    tickets_sold: int = 0
    _heap: List[Tuple[float, int, int, str, Any]] = field(default_factory=list)
    _seq: int = 0
    _waiting: Dict[str, List[int]] = field(default_factory=dict)
    _bought: Dict[str, Dict[int, int]] = field(default_factory=dict)
    _agent_orders: Dict[str, Dict[int, RefOrder]] = field(default_factory=dict)
    #: seats of expired orders: off sale until the next batch
    _limbo: Dict[str, List[Tuple[CellKey, int]]] = field(default_factory=dict)
    _order_seq: int = 0
    agents: Dict[Tuple[str, int], Agent] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------------------------
    def push(self, time: float, priority: int, kind: str, payload: Any) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (time, priority, self._seq, kind, payload))

    def settle_delay(self, key: str) -> float:
        spec = self.config.behavior.buyer_settle
        from ticketsim.rng import HashStream
        stream = HashStream(self.config.model_constants.seed, f"order:{key}:settle")
        if spec.kind == "uniform":
            low, high = spec.early_range[0], spec.late_range[1]
            return low + (high - low) * stream.uniform()
        if stream.uniform() < spec.early_frac:
            low, high = spec.early_range
        else:
            low, high = spec.late_range
        return low + (high - low) * stream.uniform()

    # -- the run --------------------------------------------------------------------------
    def run(self) -> "RefWorld":
        seed = self.config.model_constants.seed
        channels = Channels(seed)
        plans = {spec.key: resolve_cells(spec, self.config.model_constants, seed)
                 for spec in self.config.scenario.sessions}
        total_seats = sum(sum(c.capacity for c in cells) for cells in plans.values())
        for spec in self.config.scenario.sessions:
            cells = plans[spec.key]
            session = Session(spec, cells)
            self.sessions[spec.key] = session
            self._waiting[spec.key] = []
            self._bought[spec.key] = {}
            self._agent_orders[spec.key] = {}
            share = session.pool.total / total_seats if total_seats else 0.0
            count = int(round(self.config.scenario.buyers * share))
            crowd = make_crowd("object", spec.key, count, self.config, channels,
                               max_qty=spec.per_buyer_limit)
            self.crowds = getattr(self, "crowds", {})
            self.crowds[spec.key] = crowd
            # everybody is on the inventory page when the sale opens
            for index in range(len(crowd)):
                agent = Agent(index, crowd.desired(index))
                self.agents[(spec.key, index)] = agent
                self.push(spec.open_at, PRIO_BUYER_WAVE, "observe", (spec.key, agent))
            self.push(spec.open_at, PRIO_SALE_OPEN, "sale_open", spec.key)
            first_clear = spec.open_at + self.config.behavior.clear_period
            if first_clear <= self.config.scenario.horizon:
                self.push(first_clear, PRIO_CLEAR, "clear", (spec.key, 1))
        horizon = self.config.scenario.horizon
        while self._heap:
            time, _prio, _seq, kind, payload = heapq.heappop(self._heap)
            if time > horizon:
                break
            getattr(self, f"_on_{kind}")(payload, time)
        return self

    def _on_sale_open(self, key: str, now: float) -> None:
        return None

    def _request(self, key: str, agent: Agent,
                 now: float) -> Tuple[str, Optional[CellKey], int, int]:
        """Mirror of ``Engine._buyer_request``."""
        session = self.sessions[key]
        if session.spec.close_at and now >= session.spec.close_at:
            return "done", None, 0, 0
        crowd = self.crowds[key]
        bought = self._bought[key].get(agent.index, 0)
        quota = session.spec.per_buyer_limit - bought
        if quota <= 0:
            return "done", None, 0, 0
        desired = agent.desired_qty - bought
        if desired <= 0:
            return "done", None, 0, 0
        if crowd.hot(agent.index):
            cell = session.pick_hot_cell()
        else:
            cell = session.pick_random_cell(self.config.model_constants.seed,
                                            agent.index + 1, agent.attempts)
        if cell is None:
            return "empty", None, 0, 0
        observed = session.observable(cell)
        qty = min(desired, quota, observed)
        if qty <= 0:
            return "empty", None, 0, 0
        return "ok", cell, qty, observed

    def _on_observe(self, payload: Tuple[str, Agent], now: float) -> None:
        key, agent = payload
        status, cell, qty, observed = self._request(key, agent, now)
        if status != "ok":
            if status == "empty":
                self._waiting[key].append(agent.index)
            return
        agent.qty = qty
        agent.observed = observed
        if agent.attempts == 0:
            delay = self.crowds[key].first_delay(agent.index)
        else:
            delay = self.crowds[key].next_delay(agent.index, agent.attempts - 1)
        # An attempt counts when the buyer decides to send it (the engine's
        # definition): a request decided just before the end of the run counts
        # even when it would only reach the platform after the horizon.
        agent.attempts += 1
        self.attempts += 1
        self.push(now + delay, PRIO_BUYER_SUBMIT, "submit", (key, agent, cell, qty, observed))

    def _on_submit(self, payload: Tuple[str, Agent, CellKey, int, int], now: float) -> None:
        """The request is made; the platform answers ``poll_seconds`` later."""
        key, agent, cell, qty, observed = payload
        self.push(now + self.config.model_constants.poll_seconds, PRIO_BUYER_GRANT, "grant",
                  payload)

    def _on_grant(self, payload: Tuple[str, Agent, CellKey, int, int], now: float) -> None:
        key, agent, cell, qty, observed = payload
        session = self.sessions[key]
        if session.sell(cell, qty):
            self.successes += 1
            self.tickets_sold += qty
            self._bought[key][agent.index] = self._bought[key].get(agent.index, 0) + qty
            order = self._agent_orders[key].get(agent.index)
            if order is None:
                order = RefOrder(f"b:{key}:{agent.index + 1}", key, qty, now,
                                 agent_index=agent.index)
                order.seats[cell] = qty
                order.requests = 1
                self._agent_orders[key][agent.index] = order
                self._order_seq += 1
                self.orders[self._order_seq] = order
                self.push(now + self.settle_delay(order.key), PRIO_SETTLE, "settle", order)
            else:
                order.seats[cell] = order.seats.get(cell, 0) + qty
                order.qty += qty
                order.requests += 1
            self.history.append((round(now, 4), key, session.pool.remaining_total))
            # the buyer tops up when the order is short of what it wanted
            bought = self._bought[key][agent.index]
            if (bought < agent.desired_qty
                    and bought < session.spec.per_buyer_limit):
                delay = self.crowds[key].next_delay(agent.index, agent.attempts - 1)
                self.push(now + delay, PRIO_BUYER_OBSERVE, "observe", (key, agent))
        else:
            self.failures += 1
            self.push(now, PRIO_BUYER_OBSERVE, "observe", (key, agent))

    def _on_settle(self, order: RefOrder, now: float) -> None:
        seed = self.config.model_constants.seed
        session = self.sessions[order.session]
        if order.state == "CLOSED":
            return                      # already expired
        refund = hash_uniform(seed, f"order:{order.key}:refund") < self.config.behavior.refund_prob
        unpaid = (not refund and
                  hash_uniform(seed, f"order:{order.key}:unpaid") < self.config.behavior.clear_prob)
        if refund:
            for cell, qty in order.seats.items():
                session.return_seats(cell, qty, from_paid=False)
            order.state = "CLOSED"
            self.history.append((round(now, 4), order.session, session.pool.remaining_total))
            self._wake(session, now, order.qty)
            self._forget(order)
        elif unpaid:
            # never paid: cancelled when the payment window ends, seats wait for the batch
            self.push(order.placed_at + self.config.behavior.payment_window,
                      PRIO_SETTLE, "expire", order)
        else:
            for cell, qty in order.seats.items():
                session.pool.checkout(cell, qty)
            order.state = "PAID"
            self._forget(order)

    def _on_expire(self, order: RefOrder, now: float) -> None:
        """The payment window ran out: the order is cancelled, its seats go to limbo."""
        if order.state == "CLOSED":
            return
        order.state = "CLOSED"
        self._limbo.setdefault(order.session, []).extend(order.seats.items())
        self._forget(order)

    def _forget(self, order: RefOrder) -> None:
        """The buyer no longer holds this order (the engine frees the slot)."""
        if order.agent_index is not None:
            self._agent_orders.get(order.session, {}).pop(order.agent_index, None)

    def _on_clear(self, payload: Tuple[str, int], now: float) -> None:
        """A batch: the seats of expired orders go back on sale (paid orders are untouched)."""
        key, cycle = payload
        session = self.sessions[key]
        released = 0
        for cell, qty in self._limbo.pop(key, []):
            session.return_seats(cell, qty, from_paid=False)
            released += qty
        if released:
            self.history.append((round(now, 4), key, session.pool.remaining_total))
            self._wake(session, now, released)
        next_clear = session.spec.open_at + (cycle + 1) * self.config.behavior.clear_period
        if next_clear <= self.config.scenario.horizon:
            self.push(next_clear, PRIO_CLEAR, "clear", (key, cycle + 1))

    def _wake(self, session: Session, now: float, released: int) -> None:
        waiting = self._waiting.get(session.key)
        if not waiting:
            return
        if not session.pool.any_available():
            return
        budget = max(1, int(released * self.config.behavior.wake_budget_per_ticket))
        count = min(budget, len(waiting))
        woken = waiting[:count]
        del waiting[:count]
        for index in woken:
            agent = self.agents.setdefault((session.key, index),
                                           Agent(index, self.crowds[session.key].desired(index)))
            self.push(now, PRIO_BUYER_OBSERVE, "observe", (session.key, agent))


class EquivalenceTests(unittest.TestCase):
    def _compare(self, config: Config) -> None:
        from ticketsim.engine import Engine

        engine = Engine(config)
        engine.run()
        reference = RefWorld(config).run()

        self.assertEqual(engine.metrics.buyer_attempts, reference.attempts)
        self.assertEqual(engine.metrics.buyer_successes, reference.successes)
        self.assertEqual(engine.metrics.buyer_failures, reference.failures)
        self.assertEqual(engine.metrics.tickets_sold, reference.tickets_sold)
        for key, session in engine.sessions.items():
            other = reference.sessions[key]
            self.assertEqual(session.pool.remaining_total, other.pool.remaining_total, key)
            self.assertEqual(session.pool.pending_total, other.pool.pending_total, key)
            self.assertEqual(session.pool.paid_total, other.pool.paid_total, key)
        self.assertEqual(engine.pool_history, reference.history)

    def test_small_scenario(self):
        self._compare(small_config(buyers=300, seats=150, horizon=200.0))

    def test_with_refunds_and_clearing(self):
        self._compare(small_config(buyers=300, seats=150, horizon=900.0,
                                    clear_period=200.0, refund_prob=0.1, clear_prob=0.2))

    def test_with_a_refill_wave(self):
        """The parked buyers wake up when tickets come back."""
        self._compare(small_config(buyers=200, seats=60, horizon=600.0,
                                    clear_period=150.0, refund_prob=0.0, clear_prob=1.0))

    def test_with_sale_windows(self):
        """Staggered openings and an early close."""
        from ticketsim.schema import SessionSpec
        config = small_config(buyers=500, horizon=600.0, refund_prob=0.3, clear_prob=0.3,
                              clear_period=100.0)
        self._compare(config.with_scenario(sessions=(
            SessionSpec(key="a", seats=80, per_buyer_limit=2, open_at=0.0, close_at=60.0),
            SessionSpec(key="b", seats=60, per_buyer_limit=3, open_at=30.0))))


if __name__ == "__main__":
    unittest.main()
