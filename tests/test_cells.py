"""The cell model: cells, requests, orders, purchase limits.

These tests pin the mechanism down:

* a request takes tickets from **one** cell;
* an order may be made of several requests;
* every buyer is limited by the per-session purchase limit;
* the seats come back to the cell they came from;
* what a buyer sees is the decoded, per cell truth.
"""
from __future__ import annotations

import unittest

from ticketsim.engine import Engine
from ticketsim.schema import CellSpec, Config, SessionSpec, resolve_cells

from helpers import small_config


class CellResolutionTests(unittest.TestCase):
    """A9: automatic and hand written seating plans share one code path."""

    def test_auto_split_is_exact_and_deterministic(self):
        config = small_config(seats=200, cells=7)
        cells = resolve_cells(config.scenario.sessions[0], config.model_constants,
                              config.model_constants.seed)
        self.assertEqual(len(cells), 7)
        self.assertEqual(sum(c.capacity for c in cells), 200)
        again = resolve_cells(config.scenario.sessions[0], config.model_constants,
                              config.model_constants.seed)
        self.assertEqual(cells, again)

    def test_auto_split_is_not_flat(self):
        """The zipf shape gives a few big cells and many small ones."""
        config = small_config(seats=2000, cells=41)
        cells = resolve_cells(config.scenario.sessions[0], config.model_constants,
                              config.model_constants.seed)
        capacities = [c.capacity for c in cells]
        self.assertGreater(max(capacities), 2 * min(capacities))
        heats = [c.heat for c in cells]
        self.assertAlmostEqual(max(heats), 1.0 - min(capacities) / max(capacities), places=6)

    def test_manual_cells_win(self):
        spec = SessionSpec(key="s/x", seats=30, per_buyer_limit=3,
                           cells=(CellSpec(label="VIP", capacity=10, price=3800, heat=0.9),
                                  CellSpec(label="GA", capacity=20)))
        config = small_config().with_scenario(sessions=(spec,))
        cells = resolve_cells(spec, config.model_constants, config.model_constants.seed)
        self.assertEqual([c.label for c in cells], ["VIP", "GA"])
        self.assertEqual([c.capacity for c in cells], [10, 20])
        self.assertEqual(cells[0].price, 3800)
        self.assertEqual(cells[0].heat, 0.9)
        # the heat of a cell without an override is derived from its capacity
        self.assertAlmostEqual(cells[1].heat, 0.0, places=6)

    def test_seats_must_match_the_manual_cells(self):
        spec = SessionSpec(key="s/x", seats=99, cells=(CellSpec(capacity=10),))
        config = small_config().with_scenario(sessions=(spec,))
        with self.assertRaises(ValueError):
            config.validate()

    def test_json_round_trip(self):
        spec = SessionSpec(key="s/x", seats=30, per_buyer_limit=3,
                           cells=(CellSpec(label="VIP", capacity=10, price=3800, heat=0.9),
                                  CellSpec(label="GA", capacity=20)))
        config = small_config().with_scenario(sessions=(spec,))
        again = Config.from_dict(config.to_dict())
        self.assertEqual(again, config)
        self.assertEqual(again.scenario.sessions[0].cells[0].label, "VIP")
        self.assertEqual(again.scenario.sessions[0].cells[0].price, 3800)

    def test_unknown_fields_are_ignored(self):
        """A configuration with extra fields (from another version) still runs."""
        old = {
            "scenario": {"buyers": 100, "horizon": 60.0,
                         "sessions": [{"key": "s/a", "seats": 100, "target": 2,
                                        "per_buyer_limit": 2, "tier": "hot"}]},
            "behavior": {"refund_prob": 0.0, "clear_prob": 0.0, "checkout_delay": 180.0},
            "model_constants": {"seed": 5, "area_count": 6, "request_latency": 0.3},
            "something_else": {"a": 1},
        }
        config = Config.from_dict(old)
        config.validate()
        result = Engine(config).run()
        self.assertTrue(result.summary["conservation_ok"])
        self.assertEqual(result.summary["rule_violations"], 0)


class RequestMechanicsTests(unittest.TestCase):
    """One request, one cell; orders merge; the purchase limit binds everybody."""

    def test_a_request_never_spans_two_cells(self):
        engine = Engine(small_config(buyers=50, seats=40, cells=4, horizon=60.0))
        engine.run()
        for session in engine.sessions.values():
            for order in session.orders.values():
                self.assertTrue(order.seats)
                for cell, qty in order.seats.items():
                    self.assertLessEqual(qty, session.pool.capacity[cell])

    def test_an_order_can_be_made_of_several_requests(self):
        """A buyer who did not get every ticket at once tops the order up.

        Every buyer wants 4 tickets and every cell holds 1: a request can only
        take one ticket, so every order is built from several requests.
        """
        config = small_config(buyers=40, seats=40, cap=4, cells=40, horizon=300.0)
        config = config.with_constants(buyer_qty_weights=(0.0, 0.0, 0.0, 1.0),
                                       area_capacity_shape="uniform")
        engine = Engine(config)
        engine.run()
        merged = [o for s in engine.sessions.values() for o in s.orders.values()
                  if o.requests > 1]
        self.assertTrue(merged, "expected at least one order built from two requests")
        for order in merged:
            self.assertEqual(sum(order.seats.values()), order.qty)

    def test_buyers_stop_at_the_limit(self):
        """The purchase limit binds every buyer, top-ups included.

        The desired quantity is already truncated to the limit, so the limit
        is what caps a buyer; the cumulative counter makes sure that a buyer
        that tops up cannot go over it either.
        """
        for cap in (1, 2, 4):
            with self.subTest(cap=cap):
                config = small_config(buyers=300, seats=400, cap=cap, cells=3, horizon=200.0,
                                      refund_prob=0.2)
                engine = Engine(config)
                engine.run()
                for key, session in engine.sessions.items():
                    for bought in engine._bought[key]:
                        self.assertLessEqual(bought, cap)
                    for order in session.orders.values():
                        self.assertLessEqual(order.qty, cap)

    def test_seats_come_back_to_their_own_cell(self):
        config = small_config(buyers=100, seats=120, cap=2, cells=4,
                              refund_prob=1.0, horizon=200.0)
        engine = Engine(config)
        engine.run()
        for session in engine.sessions.values():
            total = sum(session.pool.remaining.values())
            self.assertGreater(total, 0)
            self.assertTrue(session.pool.check())


class ObservationTests(unittest.TestCase):
    """What the inventory page shows."""

    def test_a_big_cell_shows_the_limit_a_small_one_its_stock(self):
        spec = SessionSpec(key="s/a", seats=0, per_buyer_limit=4, cells=(
            CellSpec(label="Big", capacity=50), CellSpec(label="Small", capacity=3),
            CellSpec(label="Empty", capacity=0)))
        engine = Engine(small_config(buyers=0).with_scenario(sessions=(spec,)),
                        backend="python")
        session = engine.sessions["s/a"]
        self.assertEqual([session.observable(cell.key) for cell in session.cells], [4, 3, 0])

    def test_no_buyer_asks_for_more_than_it_saw(self):
        result = Engine(small_config(buyers=600, seats=150, cells=6, horizon=400.0)).run()
        self.assertEqual(result.summary["rule_violations"], 0)


if __name__ == "__main__":
    unittest.main()
