"""Performance regression: the speed targets of the simulator.

The bounds are deliberately generous (they run on laptops and CI machines),
but they are tight enough to catch an accidental explosion of the event
count - which is how the simulator got slow in the first place.
"""
from __future__ import annotations

import time
import unittest
from typing import Any, Tuple

from ticketsim.runner import run
from ticketsim.schema import Config, ScenarioSpec, SessionSpec


def timed(config: Config, attempts: int = 3, **kwargs: Any) -> Tuple[float, Any]:
    """The fastest of a few runs and its result.

    A run is deterministic, so the fastest attempt is the cost of the code;
    the slower ones measure whatever else the machine was doing.  A busy
    machine must not fail a regression test.
    """
    best = float("inf")
    result = None
    for _ in range(attempts):
        start = time.perf_counter()
        result = run(config, **kwargs)
        best = min(best, time.perf_counter() - start)
    return best, result


def big_config(buyers: int) -> Config:
    sessions = (
        SessionSpec(key="arena", name="Arena", seats=20000, per_buyer_limit=2),
        SessionSpec(key="stadium", name="Stadium", seats=22500, per_buyer_limit=2),
    )
    return Config(scenario=ScenarioSpec(buyers=buyers, sessions=sessions, horizon=1800.0))


class PerformanceTests(unittest.TestCase):
    """The targets (docs/ENGINEERING.md), met since the world core is in Rust.

    Measured on a laptop: see README.md (Performance).  The bounds
    leave a factor of 2-4 for slower machines and busy CI runners.
    """

    def test_buyers_are_fast(self):
        config = big_config(20_000)
        elapsed, result = timed(config)
        self.assertLess(elapsed, 0.5, f"20k buyers took {elapsed:.2f}s")
        self.assertTrue(result.summary["conservation_ok"])

    def test_a_hundred_thousand_buyers(self):
        """About the size of the baseline (127.5k buyers): < 0.5 s planned."""
        config = big_config(100_000)
        elapsed, result = timed(config)
        self.assertLess(elapsed, 1.0, f"100k buyers took {elapsed:.2f}s")
        self.assertTrue(result.summary["conservation_ok"])

    def test_a_million_buyers(self):
        """A million buyers online at once: 2-4 s planned."""
        config = big_config(1_000_000)
        elapsed, result = timed(config)
        self.assertLess(elapsed, 4.0, f"1M buyers took {elapsed:.2f}s")
        self.assertTrue(result.summary["conservation_ok"])

    def test_the_reference_engine_stays_usable(self):
        """The Python reference is for checking, not for speed - but not unbounded."""
        config = big_config(20_000)
        elapsed, result = timed(config, backend="python")
        self.assertLess(elapsed, 1.5, f"20k buyers took {elapsed:.2f}s (python)")
        self.assertTrue(result.summary["conservation_ok"])

    def test_the_event_count_stays_flat(self):
        """More buyers must not cost more than a constant per buyer.

        This is the regression that matters: the retry loop used to spend
        three events per attempt (observe → submit → grant) and one event
        per observation; now one attempt is one event.
        """
        small = run(big_config(20_000))
        large = run(big_config(100_000))
        per_buyer_small = small.metrics.buyer_attempts / 20_000
        per_buyer_large = large.metrics.buyer_attempts / 100_000
        self.assertLess(per_buyer_large, per_buyer_small * 1.5,
                        "the attempts per buyer grew with the crowd")

    def test_order_cap_reduces_the_work(self):
        full = run(big_config(20_000))
        capped = run(big_config(100_000), order_cap=2_000)
        self.assertLess(capped.metrics.buyer_attempts, full.metrics.buyer_attempts)
        self.assertEqual(capped.summary["order_cap"], 2_000)


if __name__ == "__main__":
    unittest.main()
