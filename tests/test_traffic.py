"""The platform load: every request the crowd sends, counted by stage.

The counting rules live in ``summary.traffic_summary``; these tests pin them
down so a change to the engine cannot quietly shift the load figures.
"""
from __future__ import annotations

import unittest

from ticketsim import scenarios
from ticketsim.engine import BACKENDS, Engine
from ticketsim.runner import run
from ticketsim.summary import polls_per_attempt

from helpers import small_config


def _run(**changes):
    config = scenarios.two_venues(ratio=3.0, venue_scale=0.0025)
    if changes:
        config = config.with_constants(**changes)
    return run(config)


class TrafficTests(unittest.TestCase):
    def test_the_stages_follow_the_counters(self):
        result = _run()
        stages = result.summary["traffic"]["stages"]
        m = result.metrics
        polls = result.summary["traffic"]["polls_per_attempt"]
        self.assertEqual(stages["inventory"], m.buyer_inventory_reads)
        self.assertEqual(stages["submit"], m.buyer_attempts)
        self.assertEqual(stages["poll"], m.buyer_attempts * polls)
        self.assertEqual(stages["checkout"], m.buyer_orders)
        self.assertEqual(stages["total"], sum(v for k, v in stages.items() if k != "total"))
        self.assertEqual(result.summary["traffic_total"], stages["total"])

    def test_every_request_was_preceded_by_a_look(self):
        """A buyer looks at the page before every request (and sometimes finds nothing)."""
        stages = _run().summary["traffic"]["stages"]
        self.assertGreater(stages["inventory"], stages["submit"])

    def test_the_polls_per_request(self):
        """A request waits ``poll_seconds``; the client polls every ``poll_interval``."""
        for seconds, interval, expected in ((5.0, 0.5, 10), (4.8, 0.6, 8), (5.0, 5.0, 1),
                                            (5.0, 0.0, 1), (0.1, 0.5, 1)):
            with self.subTest(seconds=seconds, interval=interval):
                config = small_config().with_constants(poll_seconds=seconds,
                                                       poll_interval=interval)
                self.assertEqual(polls_per_attempt(config), expected)

    def test_the_counts_are_integers(self):
        """Counted, not weighted: the weight of a stage is a separate question."""
        for key, value in _run().summary["traffic"]["stages"].items():
            self.assertIsInstance(value, int, key)

    def test_the_rates(self):
        summary = _run().summary
        horizon = scenarios.two_venues().scenario.horizon
        self.assertAlmostEqual(summary["requests_per_second"],
                               round(summary["traffic_total"] / horizon, 3), places=3)
        self.assertAlmostEqual(summary["requests_per_ticket"],
                               round(summary["traffic_total"] / summary["tickets_sold"], 3),
                               places=3)

    def test_both_engines_count_the_same(self):
        config = small_config(buyers=800, seats=150, refund_prob=0.2, horizon=600.0)
        totals = {backend: Engine(config, backend=backend).run().summary["traffic"]
                  for backend in BACKENDS}
        self.assertEqual(totals["python"], totals["rust"])

    def test_no_buyers_means_no_load(self):
        result = run(small_config(buyers=0))
        self.assertEqual(result.summary["traffic_total"], 0)
        self.assertIsNone(result.summary["requests_per_ticket"])


if __name__ == "__main__":
    unittest.main()
