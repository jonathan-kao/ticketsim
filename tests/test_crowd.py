"""The two crowd implementations must produce identical results.

``synthetic`` (parallel arrays) is the production path; ``object`` (one object
per buyer) is kept as the readable reference.  The comparison is exact: the
same pool trajectory, the same counters, the same buyer fingerprint.
"""
from __future__ import annotations

import time
import unittest

from ticketsim.engine import Engine
from ticketsim.runner import run

from helpers import small_config


class CrowdEquivalenceTests(unittest.TestCase):
    def _run(self, config, impl):
        # the two crowd implementations belong to the Python reference engine
        # (the Rust core has one crowd; tests/test_backends.py compares it to these)
        cfg = config.with_constants(crowd_impl=impl)
        return Engine(cfg, backend="python").run()

    def test_identical_trajectory(self):
        config = small_config(buyers=300, seats=150, horizon=400.0,
                              clear_period=150.0, refund_prob=0.1, clear_prob=0.2)
        a = self._run(config, "object")
        b = self._run(config, "synthetic")
        self.assertEqual(a.pool_history, b.pool_history)
        self.assertEqual(a.summary, b.summary)
        self.assertEqual(a.metrics.buyer_fingerprint, b.metrics.buyer_fingerprint)
        self.assertEqual(a.metrics.buyer_attempts, b.metrics.buyer_attempts)
        self.assertEqual(a.metrics.buyer_successes, b.metrics.buyer_successes)
        self.assertEqual(a.metrics.buyer_failures, b.metrics.buyer_failures)

    def test_identical_with_a_refill_wave(self):
        config = small_config(buyers=200, seats=60, horizon=600.0,
                              clear_period=150.0, refund_prob=0.0, clear_prob=1.0)
        a = self._run(config, "object")
        b = self._run(config, "synthetic")
        self.assertEqual(a.pool_history, b.pool_history)
        self.assertEqual(a.summary, b.summary)

    def test_the_synthetic_crowd_is_the_default(self):
        config = small_config()
        self.assertEqual(config.model_constants.crowd_impl, "synthetic")

    def test_unknown_implementation_is_rejected(self):
        config = small_config().with_constants(crowd_impl="nope")
        with self.assertRaises(KeyError):
            run(config)


class CrowdPerformanceTests(unittest.TestCase):
    def test_the_synthetic_crowd_is_not_slower(self):
        """The synthetic crowd exists for scale; it must not be slower."""
        config = small_config(buyers=20_000, seats=4000, horizon=300.0)
        timings = {}
        for impl in ("object", "synthetic"):
            start = time.perf_counter()
            run(config.with_constants(crowd_impl=impl), backend="python")
            timings[impl] = time.perf_counter() - start
        self.assertLess(timings["synthetic"], timings["object"] * 1.5,
                        f"synthetic {timings['synthetic']:.2f}s vs object {timings['object']:.2f}s")


if __name__ == "__main__":
    unittest.main()
