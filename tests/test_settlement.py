"""The life of an order: the decision draw, refunds, payment, expiry and the batches."""
from __future__ import annotations

import unittest

from ticketsim.engine import Engine
from ticketsim.order import OrderState

from helpers import small_config


def run_engine(config):
    engine = Engine(config)
    engine.run()
    return engine


def orders(engine):
    return [o for s in engine.sessions.values() for o in s.orders.values()]


class OrderSizeTests(unittest.TestCase):
    """A single order cannot exceed the session's purchase limit."""

    def test_orders_respect_the_session_limit(self):
        for cap in (1, 2, 4):
            engine = run_engine(small_config(cap=cap, seats=200, buyers=60))
            for session in engine.sessions.values():
                for order in session.orders.values():
                    self.assertLessEqual(order.qty, cap, (cap, session.key))

    def test_the_distribution_is_truncated_at_the_limit(self):
        from ticketsim.crowd import make_crowd
        from ticketsim.rng import Channels
        config = small_config(cap=2)
        crowd = make_crowd("object", "s/a", 400, config, Channels(config.model_constants.seed),
                            max_qty=config.scenario.sessions[0].per_buyer_limit)
        sizes = [crowd.desired(i) for i in range(len(crowd))]
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 2)
        self.assertIn(1, sizes)

    def test_the_distribution_matches_the_weights(self):
        """The order size must follow the configured weights.

        The equivalence test cannot catch this class of bug: the engine and the
        reference implementation share the same drawing code.
        """
        from collections import Counter

        from ticketsim.crowd import make_crowd
        from ticketsim.rng import Channels
        config = small_config(cap=4)
        weights = config.model_constants.buyer_qty_weights
        crowd = make_crowd("object", "s/a", 40_000, config,
                            Channels(config.model_constants.seed), max_qty=4)
        counts = Counter(crowd.desired(i) for i in range(len(crowd)))
        total = len(crowd)
        for index, weight in enumerate(weights):
            share = counts[index + 1] / total
            self.assertAlmostEqual(share, weight, delta=0.02, msg=f"qty {index + 1}")


class SettlementTests(unittest.TestCase):
    def test_a_refund_puts_the_seats_back_at_once(self):
        engine = run_engine(small_config(refund_prob=1.0))
        refunded = [o for o in orders(engine) if o.state is OrderState.CLOSED]
        self.assertTrue(refunded, "expected the refunded orders to be closed")
        self.assertEqual(engine.metrics.refunded_tickets, sum(o.qty for o in refunded))
        for session in engine.sessions.values():
            self.assertEqual(session.limbo, {})        # nothing waits for a batch
            self.assertTrue(session.pool.check())

    def test_paid_orders_are_never_taken_back(self):
        """Only unpaid orders expire; a paid order keeps its seats for good."""
        engine = run_engine(small_config(refund_prob=0.0, clear_prob=0.0, clear_period=60.0))
        for order in orders(engine):
            self.assertIsNot(order.state, OrderState.CLOSED)
        self.assertTrue([o for o in orders(engine) if o.state is OrderState.PAID])
        self.assertEqual(engine.metrics.cleared_tickets, 0)

    def test_unpaid_orders_expire_and_come_back_with_the_batch(self):
        config = small_config(refund_prob=0.0, clear_prob=1.0, clear_period=60.0,
                              payment_window=30.0, horizon=900.0)
        engine = run_engine(config)
        for order in orders(engine):
            self.assertIsNot(order.state, OrderState.PAID)
        self.assertGreater(engine.metrics.cleared_tickets, 0)
        for session in engine.sessions.values():
            self.assertEqual(session.limbo, {})
            self.assertTrue(session.pool.check())

    def test_the_seats_of_an_expired_order_wait_for_the_batch(self):
        """Between the expiry and the next batch, the seats are neither on sale nor sold."""
        config = small_config(buyers=50, seats=500, refund_prob=0.0, clear_prob=1.0,
                              clear_period=1000.0, payment_window=30.0, horizon=900.0)
        engine = run_engine(config)
        self.assertEqual(engine.metrics.cleared_tickets, 0)          # no batch yet
        waiting = sum(qty for s in engine.sessions.values()
                      for seats in s.limbo.values() for qty in seats.values())
        self.assertGreater(waiting, 0)
        for session in engine.sessions.values():
            pool = session.pool
            self.assertEqual(pool.remaining_total + pool.pending_total + pool.paid_total,
                             pool.total)

    def test_the_payment_window_starts_when_the_order_is_placed(self):
        config = small_config(buyers=60, seats=500, refund_prob=0.0, clear_prob=1.0,
                              clear_period=5000.0, payment_window=45.0, horizon=900.0)
        engine = Engine(config, trace_limit=10 ** 9)
        engine.run()
        placed = {}
        for session in engine.sessions.values():
            for order in session.orders.values():
                placed[order.id] = order.placed_at
        expired = [item for item in engine.trace if item["kind"] == "expired"]
        self.assertTrue(expired)
        earliest = min(placed.values())
        for item in expired:            # the trace rounds the time to 4 decimals
            self.assertGreaterEqual(item["t"], earliest + 45.0 - 1e-4)

    def test_returned_seats_wake_waiting_buyers(self):
        config = small_config(buyers=2000, seats=60, refund_prob=0.5, horizon=900.0)
        engine = run_engine(config)
        self.assertGreater(engine.metrics.buyer_parked, 0)
        self.assertGreater(engine.metrics.buyer_wakes, 0)


if __name__ == "__main__":
    unittest.main()
