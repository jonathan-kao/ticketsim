"""The Rust world core against the Python reference engine (docs/ENGINEERING.md).

The acceptance criterion of the port is **bit-identical output**, not "looks
about the same".  The golden fixtures cover nine scenarios; this test runs the
two backends on the same configurations and compares them **event by event**:

* the complete event stream (buyer events and unrounded times included), the
  pool trajectory, the summary, the counters;
* the world after the run: every cell's pool, every order, every buyer's
  purchases;
* every optional mechanism (noise, decision time laws, hand-written seating
  plans, staggered openings and early closes, the screening cap, ...).

It also compares the Rust building blocks with the Python originals: the keyed
hash, the crowd's draws and ``round()``.
"""
from __future__ import annotations

import hashlib
import random
import unittest
from typing import Any, Dict, List, Optional

from helpers import small_config
from ticketsim import golden, ticketsim_core
from ticketsim.crowd import make_crowd
from ticketsim.engine import BACKENDS, DEFAULT_BACKEND, Engine, PythonEngine, backend_name
from ticketsim.rng import Channels
from ticketsim.rust_engine import RustEngine
from ticketsim.schema import BuyerSettleSpec, CellSpec, Config, SessionSpec

def snapshot(engine: Any) -> Dict[str, Any]:
    """Everything a finished engine exposes, in comparable form."""
    result = engine._result
    sessions = {}
    for key, session in engine.sessions.items():
        sessions[key] = {
            "capacity": dict(session.pool.capacity),
            "remaining": dict(session.pool.remaining),
            "pending": dict(session.pool.pending),
            "paid": dict(session.pool.paid),
            "totals": (session.pool.total, session.pool.remaining_total,
                       session.pool.pending_total, session.pool.paid_total),
            "pool_ok": session.pool.check(),
            "limbo": {order: dict(seats) for order, seats in session.limbo.items()},
            "sold_out_at": session.sold_out_at,
            "orders": dict(session.orders),
            "held": session.held_tickets(),
        }
    return {
        "trace": [golden.canonical_event(item) for item in result.trace],
        "pool_history": [tuple(row) for row in result.pool_history],
        "summary": result.summary,
        "session_summary": result.sessions,
        "metrics": result.metrics,
        "sessions": sessions,
        "bought": {key: list(values) for key, values in engine._bought.items()},
        "now": engine.now,
        "events": engine.events_processed,
    }


def run_backend(backend: str, config: Config, step: Optional[int] = None,
                **kwargs: Any) -> Dict[str, Any]:
    engine = Engine(config, trace_enabled=True, trace_limit=10 ** 9, trace_buyers=True,
                    trace_exact=True, backend=backend, **kwargs)
    if step is None:
        engine.run()
    else:
        while not engine.advance(step):
            pass
    return snapshot(engine)


class BackendEquivalenceTests(unittest.TestCase):
    maxDiff = 2000

    def assert_identical(self, config: Config, **kwargs: Any) -> Dict[str, Any]:
        reference = run_backend("python", config, **kwargs)
        ported = run_backend("rust", config, **kwargs)
        for index, (a, b) in enumerate(zip(reference["trace"], ported["trace"])):
            if a != b:
                self.fail(f"event {index} differs:\n  python: {a}\n  rust:   {b}")
        self.assertEqual(len(reference["trace"]), len(ported["trace"]))
        for part in reference:
            self.assertEqual(reference[part], ported[part], part)
        return ported

    # -- the world's mechanisms -------------------------------------------------------------
    def test_refunds_clearing_and_refill_waves(self):
        for behavior in (
            dict(refund_prob=0.1, clear_prob=0.2, clear_period=150.0),
            dict(refund_prob=0.0, clear_prob=1.0, clear_period=60.0),
            dict(refund_prob=1.0, clear_prob=0.0),
            dict(refund_prob=0.3, clear_prob=0.5, clear_period=90.0, payment_window=45.0),
            dict(wake_budget_per_ticket=0.4, clear_prob=0.6, clear_period=120.0),
            dict(wake_budget_per_ticket=30.0, clear_prob=0.6, clear_period=120.0),
            dict(hot_buyer_frac=0.0), dict(hot_buyer_frac=1.0),
        ):
            with self.subTest(behavior=behavior):
                self.assert_identical(small_config(buyers=400, seats=120,
                                                   horizon=700.0, **behavior))

    def test_buyer_settlement_laws(self):
        for settle in (BuyerSettleSpec(kind="uniform"),
                       BuyerSettleSpec(early_frac=0.8, early_range=(5.0, 40.0),
                                       late_range=(100.0, 300.0))):
            with self.subTest(kind=settle.kind):
                self.assert_identical(small_config(buyers=300, seats=120, horizon=500.0,
                                                   refund_prob=0.2, buyer_settle=settle))

    def test_purchase_limits(self):
        for cap in (1, 2, 3, 4, 6):
            with self.subTest(cap=cap):
                self.assert_identical(small_config(buyers=300, seats=200, cap=cap,
                                                   horizon=400.0))

    def test_model_constants(self):
        base = small_config(buyers=300, seats=150, horizon=300.0)
        variants = [
            base.with_constants(count_jitter=0.0),
            base.with_constants(poll_seconds=0.5, poll_interval=0.0),
            base.with_constants(area_count=1),
            base.with_constants(area_count=60, area_capacity_shape="uniform"),
            base.with_constants(buyer_qty_weights=(0.2, 0.3, 0.5)),
            base.with_constants(buyer_mix=(("a", 0.5, 0.2, 0.3, 0.1), ("b", 0.2, 3.0, 1.5, 0.0),
                                           ("a", 0.3, 9.0, 0.1, 5.0))),
        ]
        for index, config in enumerate(variants):
            with self.subTest(variant=index):
                self.assert_identical(config)

    def test_sale_windows(self):
        """Staggered openings, an early close and a session that never opens."""
        sessions = (
            SessionSpec(key="s/a", seats=80, per_buyer_limit=2, open_at=0.0, close_at=40.0),
            SessionSpec(key="s/b", seats=120, per_buyer_limit=2, open_at=30.0),
            SessionSpec(key="s/c", seats=60, per_buyer_limit=2, open_at=5000.0),
        )
        config = small_config(buyers=500, horizon=400.0,
                              clear_period=100.0, clear_prob=0.4)
        self.assert_identical(config.with_scenario(sessions=sessions))

    def test_hand_written_seating_plans(self):
        spec = SessionSpec(key="s/a", seats=0, per_buyer_limit=2, cells=(
            CellSpec(label="VIP", capacity=6, price=3800, heat=0.9),
            CellSpec(label="GA", capacity=40),
            CellSpec(label="Closed", capacity=0, heat=0.9),
            CellSpec(label="Student", capacity=12, ticket_type=1, heat=0.5),
            CellSpec(label="Family", capacity=12, ticket_type=2, heat=0.5),
        ))
        other = SessionSpec(key="s/b", seats=70, per_buyer_limit=3)
        config = small_config(buyers=300, horizon=300.0,
                              refund_prob=0.3).with_scenario(sessions=(spec, other))
        self.assert_identical(config)

    def test_degenerate_worlds(self):
        for config in (
            small_config(buyers=0, horizon=60.0),
            small_config(buyers=50, seats=10, horizon=60.0),
            small_config(buyers=40, seats=1, horizon=60.0),
            small_config(buyers=10, seats=500, horizon=0.5),
            small_config(buyers=300, seats=100, horizon=3.0),
        ):
            with self.subTest(buyers=config.scenario.buyers, horizon=config.scenario.horizon):
                self.assert_identical(config)

    def test_order_cap(self):
        self.assert_identical(small_config(buyers=2000, seats=300, horizon=200.0), order_cap=150)

    def test_many_seeds(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                self.assert_identical(small_config(
                    buyers=350, seats=110, horizon=1000.0, seed=seed,
                    refund_prob=0.08, clear_prob=0.15, clear_period=260.0))

    def test_the_golden_cases(self):
        for name, config, seed in golden.CASES:
            with self.subTest(case=name):
                self.assert_identical(config.with_seed(seed))

    # -- the interface ------------------------------------------------------------------------
    def test_stepping_does_not_change_the_run(self):
        """``advance(n)`` in small steps is the same run (the live UI depends on it)."""
        config = small_config(buyers=300, seats=100, horizon=400.0,
                              clear_prob=0.5, clear_period=120.0)
        whole = run_backend("rust", config)
        for step in (1, 7, 200):
            with self.subTest(step=step):
                stepped = run_backend("rust", config, step=step)
                for part in whole:
                    self.assertEqual(whole[part], stepped[part], part)
        self.assertEqual(whole["trace"], run_backend("python", config, step=7)["trace"])

    def test_observers_and_the_trace_limit(self):
        config = small_config(buyers=200, seats=80, horizon=200.0)
        seen: Dict[str, List[Dict[str, Any]]] = {}
        clocks: Dict[str, List[float]] = {}
        for backend in BACKENDS:
            engine = Engine(config, trace_enabled=True, trace_limit=25, backend=backend)
            seen[backend], clocks[backend] = [], []
            engine.observers.append(seen[backend].append)
            # the clock is readable from inside a callback
            engine.observers.append(
                lambda item, engine=engine, log=clocks[backend]: log.append(engine.now))
            engine.run()
            self.assertEqual(len(engine.trace), 25)
            self.assertEqual(engine.trace, seen[backend][:25])
        self.assertGreater(len(seen["rust"]), 25)
        self.assertEqual(seen["python"], seen["rust"])
        self.assertEqual(clocks["python"], clocks["rust"])

    def test_the_crowd_cache_is_shared_between_variants(self):
        config = small_config(buyers=400, seats=120, horizon=300.0)
        variant = config.with_behavior(payment_window=120.0, clear_period=100.0)
        cache: Dict[Any, Any] = {}
        first = Engine(config, crowd_cache=cache, backend="rust").run()
        crowds = dict(cache)
        self.assertEqual(len(crowds), 2)                       # one crowd per session
        second = Engine(variant, crowd_cache=cache, backend="rust").run()
        self.assertEqual(crowds, cache)                        # reused, not rebuilt
        self.assertEqual(first.summary["buyer_fingerprint"], second.summary["buyer_fingerprint"])
        alone = Engine(variant, backend="rust").run()
        self.assertEqual(second.summary, alone.summary)

    def test_backend_selection(self):
        config = small_config(buyers=20, seats=20, horizon=10.0)
        self.assertEqual(DEFAULT_BACKEND, "rust")
        self.assertIsInstance(Engine(config), RustEngine)
        self.assertIsInstance(Engine(config, backend="python"), PythonEngine)
        self.assertEqual(backend_name("PYTHON"), "python")
        with self.assertRaises(ValueError):
            Engine(config, backend="fortran")

    def test_a_failing_observer_surfaces_its_error(self):
        def broken(item):
            raise RuntimeError("the observer failed")

        config = small_config(buyers=20, seats=20, horizon=10.0)
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                engine = Engine(config, backend=backend)
                engine.observers.append(broken)
                with self.assertRaisesRegex(RuntimeError, "the observer failed"):
                    engine.run()


class CorePrimitiveTests(unittest.TestCase):
    """The pieces a port gets wrong without anybody noticing."""

    def test_keyed_hash(self):
        """The same key must give the same 64 bit value as ``hashlib.blake2b``."""
        rng = random.Random(1)
        keys = [b"", b"20261106:b:arena:1:hot", "Saal/\u00e4\u4e00:7".encode("utf-8")]
        keys += [bytes(rng.getrandbits(8) for _ in range(size))
                 for size in (1, 63, 64, 127, 128, 129, 255, 256, 257, 1000)]
        for key in keys:
            expected = int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")
            self.assertEqual(ticketsim_core.hash_u64(key), expected, key)

    def test_round(self):
        """``round(x, n)`` - correctly rounded, exact ties to the even digit."""
        rng = random.Random(2)
        values = [0.0, -0.0, 0.5, 1.5, 2.5, 0.125, 0.375, 2.675, 1e-9, -1e-9, 0.00005,
                  0.00015, 1234.56785, 1799.99995, 1e15, 1e22, -3.14159265]
        values += [rng.uniform(0, 2000) for _ in range(3000)]
        values += [rng.randrange(0, 10 ** 7) / 10 ** 4 + 5e-5 for _ in range(3000)]
        values += [n / 8 for n in range(-40, 400)] + [n / 16000 for n in range(400)]
        for value in values:
            for digits in (0, 2, 3, 4, 5):
                got = ticketsim_core.py_round(value, digits)
                self.assertEqual(got.hex(), float(round(value, digits)).hex(), (value, digits))

    def test_crowd(self):
        """The Mersenne Twister channels, ``gauss`` and the retry delays."""
        for seed, session, count, cap in ((7, "s/a", 3000, 2), (20261106, "arena", 1500, 4),
                                           (0, "Saal-\u00e4", 700, 1), (2 ** 40, "s/b", 900, 3)):
            with self.subTest(seed=seed, session=session):
                config = small_config(seed=seed, cap=cap, hot_buyer_frac=0.4)
                mc = config.model_constants
                reference = make_crowd("synthetic", session, count, config, Channels(seed),
                                       max_qty=cap)
                crowd = ticketsim_core.build_crowd(str(seed), session, count, list(mc.buyer_mix),
                                             list(mc.buyer_qty_weights), mc.count_jitter,
                                             config.behavior.hot_buyer_frac, cap)
                self.assertEqual(len(crowd), len(reference))
                self.assertGreater(len(crowd), 0)
                for index in range(len(reference)):
                    self.assertEqual(crowd.desired(index), reference.desired(index))
                    self.assertEqual(crowd.first_delay(index), reference.first_delay(index))
                    self.assertEqual(crowd.hot(index), reference.hot(index))
                for index in range(0, len(reference), 37):
                    for retries in (0, 1, 2, 9, 123):
                        self.assertEqual(crowd.next_delay(index, retries),
                                         reference.next_delay(index, retries))

    def test_the_clock_starts_at_zero(self):
        engine = Engine(small_config(), backend="rust")
        self.assertEqual(engine.now, 0.0)
        self.assertEqual(engine.events_processed, 0)


if __name__ == "__main__":
    unittest.main()
