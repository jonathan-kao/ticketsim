#!/usr/bin/env python3
"""Measure how sensitive the outcome is to every parameter.

    python3 scripts/sensitivity.py --seeds 0-4 > results/SENSITIVITY.md

Every variant changes one parameter of the baseline and runs on the same
seeds (paired), so the rows compare with each other.  The output is a
markdown table; docs/PARAMETERS.md quotes it.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ticketsim import scenarios                                     # noqa: E402
from ticketsim.runner import compare, make_executor                 # noqa: E402
from ticketsim.schema import BuyerSettleSpec, Config                # noqa: E402

COLUMNS = (("sell_through", "sell-through", "{:.4f}"),
           ("first_sold_out_at", "first sell-out (s)", "{:.1f}"),
           ("buyer_failure_rate", "refused share", "{:.3f}"),
           ("buyer_stale_failures", "refused: stale", "{:,.0f}"),
           ("traffic_total", "platform requests", "{:,.0f}"))


def variants(base: Config) -> List[Tuple[str, str, Config]]:
    """(parameter, value, config) triples to measure."""
    out: List[Tuple[str, str, Config]] = [("baseline", "-", base)]

    def add(param: str, values, change) -> None:
        for value in values:
            out.append((param, str(value), change(value)))

    add("scenario.buyers", (42500, 127500, 510000), lambda n: base.with_scenario(buyers=n))
    add("sessions[].per_buyer_limit", (1, 2, 4), lambda cap: base.with_sessions(per_buyer_limit=cap))
    add("behavior.refund_prob", (0.0, 0.03, 0.10), lambda x: base.with_behavior(refund_prob=x))
    add("behavior.clear_prob", (0.0, 0.08, 0.20), lambda y: base.with_behavior(clear_prob=y))
    add("behavior.payment_window", (300.0, 600.0, 900.0),
        lambda w: base.with_behavior(payment_window=w))
    add("behavior.clear_period", (240.0, 720.0, 1440.0),
        lambda p: base.with_behavior(clear_period=p))
    add("behavior.buyer_settle.kind", ("deadline_weighted", "uniform"),
        lambda kind: base.with_behavior(buyer_settle=replace(BuyerSettleSpec(), kind=kind)))
    add("behavior.hot_buyer_frac", (0.0, 0.30, 1.0),
        lambda f: base.with_behavior(hot_buyer_frac=f))
    add("behavior.wake_budget_per_ticket", (1.0, 3.0, 30.0),
        lambda b: base.with_behavior(wake_budget_per_ticket=b))
    add("model_constants.area_count", (1, 40, 200), lambda n: base.with_constants(area_count=n))
    add("model_constants.area_capacity_shape", ("zipf", "uniform"),
        lambda s: base.with_constants(area_capacity_shape=s))
    add("model_constants.area_capacity_spread", (0.5, 1.1, 2.0),
        lambda s: base.with_constants(area_capacity_spread=s))
    add("model_constants.area_heat_weight", (0.0, 1.0), lambda w: base.with_constants(area_heat_weight=w))
    add("model_constants.poll_seconds", (0.5, 5.0, 20.0), lambda p: base.with_constants(poll_seconds=p))
    add("model_constants.count_jitter", (0.0, 0.1), lambda j: base.with_constants(count_jitter=j))
    return out


def seed_list(text: str) -> List[int]:
    seeds: List[int] = []
    for part in text.split(","):
        if "-" in part:
            low, high = part.split("-", 1)
            seeds.extend(range(int(low), int(high) + 1))
        elif part.strip():
            seeds.append(int(part))
    return seeds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure the parameter sensitivity")
    parser.add_argument("--preset", default="baseline")
    parser.add_argument("--seeds", default="0-4")
    parser.add_argument("--jobs", type=int, default=0,
                        help="worker processes (0 = one per core, 1 = serial)")
    args = parser.parse_args(argv)

    seeds = seed_list(args.seeds)
    rows = variants(scenarios.build(args.preset))
    labelled: Dict[str, Config] = {f"{param}={value}": config for param, value, config in rows}
    executor = make_executor(args.jobs)
    try:
        aggregate = compare(labelled, seeds, executor=executor)["aggregate"]
    finally:
        if executor is not None:
            executor.shutdown()

    print(f"# Parameter sensitivity\n\nScenario `{args.preset}`, seeds {seeds[0]}-{seeds[-1]}: "
          "every value is the mean over the seeds.  Written by `scripts/sensitivity.py`.\n")
    print("| parameter | value | " + " | ".join(label for _k, label, _f in COLUMNS) + " |")
    print("|---|---|" + "---|" * len(COLUMNS))
    for param, value, _config in rows:
        row = aggregate[f"{param}={value}"]
        cells = ["-" if row.get(key) is None else fmt.format(row[key]) for key, _l, fmt in COLUMNS]
        print(f"| `{param}` | {value} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
