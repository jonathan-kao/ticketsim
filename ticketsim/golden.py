"""Golden fixtures: the **executable specification** of the simulator.

A port (the Rust world core) or a refactoring cannot be checked against
comments: comments rot, and nobody can verify their work against them.  The
fixtures are a specification that can be run:

* a fixed set of small scenarios (``CASES``);
* every run produces the **complete event stream** (every observation,
  request and answer of every buyer, every refund, expiry and batch) plus the
  pool trajectory and the summary;
* a **language-independent** serialisation (``canonical``): floats as their
  IEEE-754 bytes, sorted keys, so the Rust and the Python engine produce
  byte-identical output.

Usage::

    python3 scripts/make_golden.py            # write tests/golden/*.json
    python3 scripts/make_golden.py --check    # only check (no writing)
    python3 -m unittest tests.test_golden     # the regression test
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any, Dict, List, Tuple

from . import scenarios
from .engine import Engine
from .schema import BuyerSettleSpec, CellSpec, Config, SessionSpec

#: where the fixtures live (relative to the package)
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tests", "golden")


def f64(value: float) -> str:
    """A float as its IEEE-754 bit pattern - the same text in every language.

    ``repr(0.1)`` differs between languages (``0.1`` vs ``0.10000000000000001``);
    the bit pattern does not.  Times are the most order-sensitive part of a
    simulation, so they are compared bit by bit.
    """
    return "f64:" + struct.pack("<d", float(value)).hex()


def canonical(value: Any) -> Any:
    """A JSON-safe, order-stable, language-independent form of ``value``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return f64(value)
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
    raise TypeError(f"not canonicalisable: {value!r}")


def canonical_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """One trace event, canonicalised.

    The rounded ``t`` (for the UI) is replaced by the exact one (``t_exact``)
    - the rounded value would hide differences below 0.1 ms.
    """
    out: Dict[str, Any] = {}
    for key in sorted(item, key=str):
        if key == "t_exact":
            continue
        if key == "t":
            out["t"] = f64(item.get("t_exact", item["t"]))
            continue
        out[key] = canonical(item[key])
    return out


def run_case(config: Config, seed: int) -> Dict[str, Any]:
    """Run one case and return its canonical payload."""
    cfg = config.with_seed(seed)
    engine = Engine(cfg, trace_enabled=True, trace_limit=10 ** 9,
                    trace_buyers=True, trace_exact=True)
    result = engine.run()
    return {
        "trace": [canonical_event(item) for item in result.trace],
        "pool_history": [canonical(list(row)) for row in result.pool_history],
        "summary": canonical(result.summary),
    }


def payload_bytes(payload: Dict[str, Any]) -> bytes:
    """The canonical byte form of a payload (stable across runs and languages)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(payload_bytes(payload)).hexdigest()


def _small(**kwargs: Any) -> Config:
    """The baseline, shrunk so a golden run stays small and fast.

    About 100 seats and three times as many buyers: big enough for every
    mechanism to happen, small enough to keep the fixture readable.
    """
    base = dict(ratio=3.0, venue_scale=0.0025, horizon=1500.0)
    base.update(kwargs)
    return scenarios.two_venues(**base)


def _cases() -> List[Tuple[str, Config, int]]:
    """The fixtures: (name, config, seed).

    Every case exercises a different part of the model:

    * the opening rush, top-ups, refunds and batches (``baseline``, two seeds);
    * high demand: retries after the sell-out, waking budget (``pressure``);
    * a limit of 4: orders made of several requests (``limit_4``);
    * many returned seats in short batches (``returns``);
    * the other decision time law (``uniform_decisions``);
    * one big cell, every request competes for it (``one_cell``);
    * staggered openings and an early close (``sale_windows``);
    * a hand-written seating plan with ticket types (``seating_plan``).
    """
    returns = _small().with_behavior(refund_prob=0.2, clear_prob=0.3, clear_period=200.0,
                                     payment_window=150.0)
    windows = _small()
    windows = windows.with_scenario(sessions=(
        SessionSpec(key="early", name="Early", seats=60, per_buyer_limit=2, open_at=0.0,
                    close_at=300.0),
        SessionSpec(key="late", name="Late", seats=80, per_buyer_limit=2, open_at=120.0),
    ))
    plan = _small().with_scenario(sessions=(
        SessionSpec(key="hall", name="Hall", seats=0, per_buyer_limit=3, cells=(
            CellSpec(label="Front", capacity=8, price=3800, heat=0.9),
            CellSpec(label="Floor", capacity=40),
            CellSpec(label="Closed", capacity=0, heat=0.9),
            CellSpec(label="Student", capacity=12, ticket_type=1, heat=0.5),
            CellSpec(label="Senior", capacity=12, ticket_type=2, heat=0.5),
        )),
    ))
    return [
        ("baseline", _small(), 0),
        ("baseline_seed1", _small(), 1),
        ("pressure", _small(ratio=8.0, venue_scale=0.0012), 0),
        ("limit_4", _small(cap=4), 0),
        ("returns", returns, 0),
        ("uniform_decisions", _small().with_behavior(
            buyer_settle=BuyerSettleSpec(kind="uniform")), 0),
        ("one_cell", _small().with_constants(area_count=1), 0),
        ("sale_windows", windows, 0),
        ("seating_plan", plan, 0),
    ]


#: the golden cases (name, config, seed)
CASES = _cases()


def case_path(name: str) -> str:
    return os.path.join(GOLDEN_DIR, f"{name}.json")


def build_fixture(name: str, config: Config, seed: int) -> Dict[str, Any]:
    """The complete fixture of one case (what lands on disk)."""
    payload = run_case(config, seed)
    return {
        "name": name,
        "seed": seed,
        "config": config.with_seed(seed).to_dict(),
        "payload": payload,
        "sha256": payload_sha256(payload),
    }


def load_fixture(name: str) -> Dict[str, Any]:
    with open(case_path(name), "r", encoding="utf-8") as handle:
        return json.load(handle)
