"""The runner: the crowd cache and the parallel path must not change anything.

A report runs hundreds of scenarios; the runs are spread over several
processes and the crowd is reused between the variants of one seed.  Both are
pure optimisations - the numbers have to be exactly the ones a serial run
without the cache produces.
"""
from __future__ import annotations

import unittest

from ticketsim import scenarios
from ticketsim.runner import aggregate, compare, make_executor, run


def small_config(buyers: int = 4_000):
    """The baseline with a smaller crowd (the test stays fast)."""
    return scenarios.build("baseline").with_scenario(buyers=buyers)


class RunnerTests(unittest.TestCase):
    def test_the_crowd_cache_does_not_change_the_result(self):
        config = small_config()
        plain = run(config)
        cache = {}
        cached = run(config, crowd_cache=cache)
        again = run(config.with_behavior(payment_window=300.0), crowd_cache=cache)
        alone = run(config.with_behavior(payment_window=300.0))
        self.assertEqual(plain.summary, cached.summary)
        self.assertEqual(again.summary, alone.summary)

    def test_a_cached_crowd_is_not_reused_for_another_crowd(self):
        cache = {}
        run(small_config(), crowd_cache=cache)
        other = small_config().with_behavior(hot_buyer_frac=0.9)
        self.assertEqual(run(other, crowd_cache=cache).summary, run(other).summary)

    def test_the_parallel_path_gives_the_same_numbers(self):
        variants = {"a": small_config(), "b": small_config().with_sessions(per_buyer_limit=4)}
        seeds = (0, 1)
        serial = compare(variants, seeds)
        executor = make_executor(2)
        try:
            parallel = compare(variants, seeds, executor=executor)
        finally:
            if executor is not None:
                executor.shutdown()
        self.assertEqual(serial["aggregate"], parallel["aggregate"])
        self.assertEqual(serial["aggregate"]["a"]["n"], 2)

    def test_the_aggregate_is_the_mean(self):
        rows = [run(small_config().with_seed(seed)).summary for seed in (0, 1, 2)]
        result = aggregate(rows)
        expected = sum(row["buyer_attempts"] for row in rows) / 3
        self.assertAlmostEqual(result["buyer_attempts"], round(expected, 4))
        self.assertIn("traffic_poll", result)


if __name__ == "__main__":
    unittest.main()
