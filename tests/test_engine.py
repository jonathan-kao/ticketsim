"""The engine: event order, the buyers' life cycle, orders and the summary."""
from __future__ import annotations

import unittest

from ticketsim.engine import BACKENDS, Engine
from ticketsim.order import OrderState
from ticketsim.runner import run
from ticketsim.schema import SessionSpec

from helpers import small_config


class LifeCycleTests(unittest.TestCase):
    def test_a_run_produces_every_kind_of_event(self):
        config = small_config(buyers=600, seats=150, horizon=900.0, refund_prob=0.2,
                              clear_prob=0.3, clear_period=200.0, payment_window=150.0)
        engine = Engine(config, trace_buyers=True, trace_limit=10 ** 9)
        engine.run()
        kinds = {item["kind"] for item in engine.trace}
        for kind in ("sale_open", "buyer_wave", "buyer_req", "buyer_grant", "buyer_park",
                     "buyer_wake", "refund", "unpaid", "expired", "cleared", "checkout"):
            self.assertIn(kind, kinds)

    def test_every_order_ends_in_a_known_state(self):
        engine = Engine(small_config(refund_prob=0.2, clear_prob=0.2, horizon=900.0))
        engine.run()
        for session in engine.sessions.values():
            for order in session.orders.values():
                self.assertIn(order.state, set(OrderState))
                if order.state is OrderState.PAID:
                    self.assertIsNotNone(order.settle_at)

    def test_one_open_order_per_buyer(self):
        """A buyer's purchases merge into its open order; a new one only opens later."""
        engine = Engine(small_config(buyers=500, seats=120, cap=4, cells=8, horizon=400.0),
                        backend="python")
        engine.run()
        for session in engine.sessions.values():
            open_orders = {}
            for order in session.orders.values():
                if order.state is OrderState.PENDING:
                    self.assertNotIn(order.buyer_index, open_orders)
                    open_orders[order.buyer_index] = order.id

    def test_nobody_buys_before_the_opening_or_after_the_close(self):
        sessions = (SessionSpec(key="early", seats=80, per_buyer_limit=2, open_at=0.0,
                                close_at=40.0),
                    SessionSpec(key="late", seats=80, per_buyer_limit=2, open_at=60.0))
        config = small_config(buyers=800, horizon=400.0, refund_prob=0.3).with_scenario(
            sessions=sessions)
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                engine = Engine(config, trace_buyers=True, trace_limit=10 ** 9, backend=backend)
                result = engine.run()
                for item in result.trace:
                    if item["kind"] != "buyer_req":
                        continue
                    if item["session"] == "early":
                        self.assertLess(item["t"], 40.0)
                    else:
                        self.assertGreaterEqual(item["t"], 60.0)

    def test_a_session_that_never_opens_sells_nothing(self):
        sessions = (SessionSpec(key="now", seats=50, per_buyer_limit=2),
                    SessionSpec(key="never", seats=50, per_buyer_limit=2, open_at=5000.0))
        result = run(small_config(buyers=300).with_scenario(sessions=sessions))
        self.assertEqual(result.sessions["never"]["held"], 0)
        self.assertEqual(result.sessions["never"]["remaining"], 50)
        self.assertGreater(result.sessions["now"]["held"], 0)


class OutcomeTests(unittest.TestCase):
    def test_with_abundant_stock_every_buyer_is_served(self):
        result = run(small_config(buyers=40, seats=5000, horizon=120.0))
        summary = result.summary
        self.assertEqual(summary["buyer_failures"], 0)
        self.assertEqual(summary["buyer_successes"], summary["buyer_attempts"])
        self.assertEqual(summary["sessions_sold_out"], 0)

    def test_a_large_crowd_sells_out(self):
        result = run(small_config(buyers=3000, seats=200, horizon=300.0))
        summary = result.summary
        self.assertEqual(summary["sessions_sold_out"], 2)
        self.assertIsNotNone(summary["first_sold_out_at"])
        self.assertGreater(summary["buyer_failures"], 0)
        self.assertGreater(summary["buyer_stale_failures"], 0)

    def test_the_summary_agrees_with_the_sessions(self):
        result = run(small_config(refund_prob=0.1, clear_prob=0.2, horizon=900.0))
        summary, sessions = result.summary, result.sessions
        self.assertEqual(summary["tickets_held"], sum(s["held"] for s in sessions.values()))
        self.assertEqual(summary["stranded_stock"], sum(s["remaining"] for s in sessions.values()))
        self.assertEqual(summary["refunded"], sum(s["refunded"] for s in sessions.values()))
        self.assertEqual(summary["cleared"], sum(s["cleared"] for s in sessions.values()))
        self.assertEqual(summary["buyer_attempts"],
                         sum(s["buyer_attempts"] for s in sessions.values()))
        self.assertEqual(summary["buyer_attempts"],
                         summary["buyer_successes"] + summary["buyer_failures"])
        self.assertEqual(summary["sell_through"],
                         round(summary["tickets_held"] / summary["seats"], 4))

    def test_the_stale_failures_are_counted_consistently(self):
        result = run(small_config())
        per_session = sum(s["stale_failures"] for s in result.sessions.values())
        self.assertEqual(per_session, result.metrics.buyer_stale_failures)

    def test_the_html_report_renders(self):
        from ticketsim.report import render
        result = run(small_config(buyers=2000, seats=100, clear_prob=0.5, clear_period=100.0,
                                  payment_window=30.0, horizon=300.0), trace=True)
        page = render(result)
        self.assertIn("<svg", page)
        self.assertIn("sell-through", page)
        self.assertIn("returned", page)


class ConfigTests(unittest.TestCase):
    def test_invalid_configurations_are_rejected(self):
        cases = [
            dict(buyers=-1), dict(horizon=0.0),
        ]
        for changes in cases:
            with self.subTest(**changes):
                with self.assertRaises(ValueError):
                    small_config().with_scenario(**changes).validate()
        with self.assertRaises(ValueError):
            small_config(refund_prob=1.5).validate()
        with self.assertRaises(ValueError):
            small_config().with_constants(area_count=0).validate()
        with self.assertRaises(ValueError):
            small_config().with_sessions(close_at=-1.0, open_at=0.0).validate()

    def test_the_json_round_trip_is_exact(self):
        from ticketsim.schema import Config
        from ticketsim import scenarios
        for name in scenarios.names():
            config = scenarios.build(name)
            self.assertEqual(Config.from_json(config.to_json()), config)


if __name__ == "__main__":
    unittest.main()
