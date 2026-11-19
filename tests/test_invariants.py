"""Invariants of the world: conservation, monotonicity, caps, reproducibility."""
from __future__ import annotations

import unittest

from ticketsim.crowd import make_crowd
from ticketsim.pool import CellPool
from ticketsim.rng import Channels, HashStream
from ticketsim.engine import Engine
from ticketsim.runner import run

from helpers import small_config


class PoolTests(unittest.TestCase):
    """The pool is per cell - a request takes tickets from one cell only."""

    def _pool(self, capacities=(10, 4, 6)):
        from ticketsim.schema import ResolvedCell
        cells = tuple(ResolvedCell(key=(i, 0), label=f"c{i}", capacity=cap, price=None, heat=0.0)
                      for i, cap in enumerate(capacities))
        return CellPool(cells)

    def test_conservation(self):
        pool = self._pool()
        self.assertTrue(pool.take((0, 0), 4))
        pool.checkout((0, 0), 4)
        self.assertTrue(pool.check())
        pool.release((0, 0), 2, from_paid=True)
        self.assertTrue(pool.check())
        self.assertEqual(pool.remaining_total, 18)

    def test_no_partial_fill(self):
        pool = self._pool()
        self.assertFalse(pool.take((0, 0), 11))
        self.assertEqual(pool.remaining_total, 20)
        self.assertTrue(pool.check())

    def test_a_request_cannot_span_two_cells(self):
        """The cell is exhausted; the other cell still has tickets."""
        pool = self._pool()
        self.assertTrue(pool.take((0, 0), 10))
        self.assertFalse(pool.take((0, 0), 1))
        self.assertFalse(pool.take((1, 0), 5))
        self.assertTrue(pool.take((2, 0), 5))
        self.assertTrue(pool.check())

    def test_seats_return_to_their_own_cell(self):
        pool = self._pool()
        pool.take((2, 0), 3)
        pool.release((2, 0), 3, from_paid=False)
        self.assertEqual(pool.cell_remaining((2, 0)), 6)
        self.assertEqual(pool.cell_remaining((0, 0)), 10)
        self.assertTrue(pool.check())

    def test_monotonicity(self):
        """remaining only shrinks on a purchase and grows on a return."""
        pool = self._pool()
        levels = [pool.remaining_total]
        pool.take((0, 0), 2)
        levels.append(pool.remaining_total)
        pool.checkout((0, 0), 2)
        levels.append(pool.remaining_total)
        pool.release((0, 0), 2, from_paid=True)
        levels.append(pool.remaining_total)
        self.assertEqual(levels, [20, 18, 18, 20])


class RunInvariantTests(unittest.TestCase):
    def test_pool_conservation_after_run(self):
        result = run(small_config())
        for session in result.sessions.values():
            self.assertTrue(session["pool_ok"], session)

    def test_ticket_accounting(self):
        """Every granted ticket is in an order; every closed order was cancelled or expired.

        granted = held (pending + paid) + cancelled + expired, and an expired
        order's seats are either back on sale (cleared) or waiting for the
        next batch (limbo).
        """
        for backend in ("python", "rust"):
            with self.subTest(backend=backend):
                engine = Engine(small_config(refund_prob=0.2, clear_prob=0.3, horizon=900.0,
                                             clear_period=250.0), backend=backend)
                result = engine.run()
                orders = [o for s in engine.sessions.values() for o in s.orders.values()]
                self.assertEqual(sum(o.qty for o in orders), result.summary["tickets_sold"])
                held = sum(o.qty for o in orders if o.live)
                closed = sum(o.qty for o in orders if not o.live)
                waiting = sum(qty for s in engine.sessions.values()
                              for seats in s.limbo.values() for qty in seats.values())
                self.assertEqual(held, result.summary["tickets_held"])
                self.assertEqual(closed, result.summary["refunded"] + result.summary["cleared"]
                                 + waiting)

    def test_held_plus_on_sale_never_exceeds_the_seats(self):
        result = run(small_config(refund_prob=0.2, clear_prob=0.3, horizon=900.0))
        for session in result.sessions.values():
            self.assertLessEqual(session["held"] + session["remaining"], session["seats"])

    def test_purchases_never_give_the_limit_back(self):
        """A cancelled or expired order does not give its buyer the quota back."""
        engine = Engine(small_config(cap=1, refund_prob=0.5, clear_prob=0.3, horizon=900.0),
                        backend="python")
        engine.run()
        for key in engine.sessions:
            self.assertTrue(all(0 <= count <= 1 for count in engine._bought[key]))

    def test_reproducibility(self):
        config = small_config()
        first = run(config)
        second = run(config)
        self.assertEqual(first.summary, second.summary)

    def test_seed_changes_the_outcome(self):
        first = run(small_config(seed=1))
        second = run(small_config(seed=2))
        self.assertNotEqual(first.summary["buyer_successes"], second.summary["buyer_successes"])

    def test_common_random_numbers_between_platform_rules(self):
        """Changing a platform rule changes the outcome, not the crowd."""
        config = small_config(refund_prob=0.1, clear_prob=0.2, horizon=900.0)
        a = run(config)
        b = run(config.with_behavior(payment_window=120.0, clear_period=150.0))
        self.assertNotEqual(a.summary["cleared"], b.summary["cleared"])     # two worlds
        self.assertEqual(a.metrics.buyer_fingerprint, b.metrics.buyer_fingerprint)


class RandomnessTests(unittest.TestCase):
    def test_the_crowd_is_deterministic(self):
        config = small_config()
        first = make_crowd("object", "s/a", 50, config, Channels(config.model_constants.seed))
        second = make_crowd("object", "s/a", 50, config, Channels(config.model_constants.seed))
        self.assertEqual(len(first), len(second))
        for i in range(len(first)):
            self.assertEqual(first.desired(i), second.desired(i))
            self.assertEqual(first.first_delay(i), second.first_delay(i))

    def test_every_buyer_arrives_when_the_sale_opens(self):
        """The crowd is already online; only the submit delay separates them."""
        config = small_config()
        crowd = make_crowd("synthetic", "s/a", 50, config, Channels(config.model_constants.seed))
        self.assertGreaterEqual(len(crowd), 45)   # ±10 % count jitter
        self.assertLessEqual(len(crowd), 55)
        self.assertTrue(all(crowd.first_delay(i) > 0.0 for i in range(len(crowd))))

    def test_hash_stream_is_order_independent(self):
        """The same key always yields the same numbers (common random numbers)."""
        a = HashStream(11, "order:x:refund").uniform()
        _ = HashStream(11, "other").uniform()
        b = HashStream(11, "order:x:refund").uniform()
        self.assertEqual(a, b)

    def test_hash_stream_draws_are_sequenced(self):
        stream = HashStream(11, "order:x")
        self.assertNotEqual(stream.uniform(), stream.uniform())


if __name__ == "__main__":
    unittest.main()
