"""Golden fixtures: the simulator's **executable specification**.

Every refactoring (and the Rust port above all) must reproduce these fixtures
**byte for byte**.  This test is that gate.

    python3 scripts/make_golden.py      # write / update
    python3 -m unittest tests.test_golden
"""
from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict, List

from ticketsim import golden


def _first_difference(a: List[Any], b: List[Any]) -> str:
    """Where do two traces differ? (the first differing element)"""
    for index in range(min(len(a), len(b))):
        if a[index] != b[index]:
            return (f"element {index} differs:\n"
                    f"    stored: {json.dumps(a[index], ensure_ascii=False)}\n"
                    f"    now:    {json.dumps(b[index], ensure_ascii=False)}")
    if len(a) != len(b):
        longer, name = (a, "stored") if len(a) > len(b) else (b, "now")
        return f"length differs ({len(a)} vs {len(b)}); first extra in {name}: " \
               f"{json.dumps(longer[min(len(a), len(b))], ensure_ascii=False)}"
    return "identical"


class GoldenTests(unittest.TestCase):
    def test_every_case_matches_its_fixture(self):
        """Every case reproduces its fixture (event stream, pool trajectory, summary)."""
        for name, config, seed in golden.CASES:
            with self.subTest(case=name):
                path = golden.case_path(name)
                if not os.path.exists(path):
                    self.fail(f"missing fixture {path} - run scripts/make_golden.py")
                with open(path, "r", encoding="utf-8") as handle:
                    stored = json.load(handle)
                payload = golden.run_case(config, seed)
                self.assertEqual(
                    golden.payload_sha256(payload), stored.get("sha256"),
                    f"{name}: the run differs from the fixture\n"
                    f"  {_first_difference(stored['payload']['trace'], payload['trace'])}")

    def test_the_fixtures_are_intact(self):
        """The sha256 of a fixture matches its content (no hand edits, no truncation)."""
        for name, _config, _seed in golden.CASES:
            with self.subTest(case=name):
                path = golden.case_path(name)
                if not os.path.exists(path):
                    self.fail(f"missing fixture {path} - run scripts/make_golden.py")
                with open(path, "r", encoding="utf-8") as handle:
                    stored = json.load(handle)
                self.assertEqual(golden.payload_sha256(stored["payload"]),
                                 stored.get("sha256"), f"{name}: the fixture is damaged")

    def test_the_opening_rush_does_not_depend_on_later_rules(self):
        """Common random numbers: the same seed means the same crowd.

        ``baseline`` and ``uniform_decisions`` only differ in *when* buyers
        decide about their orders, which happens long after the opening, so
        the opening rush (every request at t = 0) must be identical.
        """
        by_name = {name: (config, seed) for name, config, seed in golden.CASES}

        def opening(name: str) -> List[Dict[str, Any]]:
            config, seed = by_name[name]
            payload = golden.run_case(config, seed)
            first = payload["trace"][0]["t"]
            return [event for event in payload["trace"]
                    if event["kind"] == "buyer_req" and event["t"] == first]

        rush = opening("baseline")
        self.assertGreater(len(rush), 100)
        self.assertEqual(rush, opening("uniform_decisions"),
                         "the crowd differs between two scenarios of the same seed")


if __name__ == "__main__":
    unittest.main()
