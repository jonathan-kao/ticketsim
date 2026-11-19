#!/usr/bin/env python3
"""Run every scenario and write ``results/REPORT.md``.

    python3 scripts/run_experiments.py --seeds 0-9 --jobs 0

Everything in the report is produced by this script; no number is typed in by
hand.  Every scenario changes one thing against the baseline, and every
scenario runs on the same seeds, so the rows compare pairwise.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ticketsim import scenarios                                     # noqa: E402
from ticketsim.runner import compare, make_executor                 # noqa: E402
from ticketsim.schema import Config                                 # noqa: E402

#: what every scenario changes against the baseline
NOTES = {
    "baseline": "two venues on sale at once, crowd = 3x the stock, limit 2",
    "demand_20x": "crowd = 20x the stock (venues scaled to 1/10, same ratio)",
    "demand_60x": "crowd = 60x the stock (venues scaled to 1/10, same ratio)",
    "demand_120x": "crowd = 120x the stock (venues scaled to 1/10, same ratio)",
    "limit_1": "purchase limit 1 per buyer and session",
    "limit_4": "purchase limit 4 per buyer and session",
    "payment_window_300": "unpaid orders expire after 5 minutes",
    "payment_window_900": "unpaid orders expire after 15 minutes",
    "batch_every_240": "returned seats go back on sale every 4 minutes",
    "batch_every_1440": "returned seats go back on sale every 24 minutes",
    "cells_1": "the seating plan is one single cell",
    "cells_200": "the seating plan has 200 cells",
    "uniform_decisions": "buyers decide uniformly over the payment window",
}

COLUMNS = (
    ("sell_through", "sell-through", "{:.4f}"),
    ("first_sold_out_at", "first sell-out (s)", "{:.1f}"),
    ("stranded_stock", "unsold", "{:.1f}"),
    ("buyer_attempts", "requests", "{:,.0f}"),
    ("buyer_failure_rate", "refused share", "{:.3f}"),
    ("buyer_stale_failures", "refused: stale", "{:,.0f}"),
    ("traffic_total", "platform requests", "{:,.0f}"),
    ("requests_per_ticket", "requests / ticket", "{:.1f}"),
)


def seed_list(text: str) -> List[int]:
    seeds: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            seeds.extend(range(int(low), int(high) + 1))
        else:
            seeds.append(int(part))
    return seeds


def table(rows: Dict[str, Dict[str, float]]) -> str:
    lines = ["| scenario | " + " | ".join(label for _key, label, _fmt in COLUMNS) + " |",
             "|---|" + "---|" * len(COLUMNS)]
    for name, row in rows.items():
        cells = []
        for key, _label, fmt in COLUMNS:
            value = row.get(key)
            cells.append("-" if value is None else fmt.format(value))
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def traffic_table(rows: Dict[str, Dict[str, float]]) -> str:
    stages = ("inventory", "submit", "poll", "checkout")
    lines = ["| scenario | " + " | ".join(stages) + " | total | requests / s |",
             "|---|" + "---|" * (len(stages) + 2)]
    for name, row in rows.items():
        cells = [f"{row.get('traffic_' + stage, 0):,.0f}" for stage in stages]
        lines.append(f"| `{name}` | " + " | ".join(cells) +
                     f" | {row.get('traffic_total', 0):,.0f} | {row.get('requests_per_second', 0):,.1f} |")
    return "\n".join(lines)


def constants_table(config: Config) -> str:
    lines = ["| parameter | value | source |", "|---|---|---|"]
    for name, value, source in config.constants_table():
        lines.append(f"| {name} | `{value}` | {source} |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run every scenario and write the report")
    parser.add_argument("--seeds", default="0-9")
    parser.add_argument("--order-cap", type=int, default=None,
                        help="cap the buyers per session (coarse screening; changes the statistics)")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "..", "results", "REPORT.md"))
    parser.add_argument("--jobs", type=int, default=0,
                        help="worker processes (0 = one per core, 1 = serial)")
    args = parser.parse_args(argv)

    seeds = seed_list(args.seeds)
    variants = {name: scenarios.build(name) for name in scenarios.names()}
    executor = make_executor(args.jobs)
    try:
        payload = compare(variants, seeds, order_cap=args.order_cap, executor=executor)
    finally:
        if executor is not None:
            executor.shutdown()
    rows = payload["aggregate"]

    out: List[str] = [
        "# ticketsim - scenario report\n",
        "> Written by `scripts/run_experiments.py`; every number comes from a run, "
        "none is typed in by hand.\n",
        f"Seeds {seeds[0]}-{seeds[-1]} ({len(seeds)} runs per scenario, the same seeds for "
        "every scenario: the rows compare pairwise).  Every value is the mean over the seeds.\n",
    ]
    if args.order_cap:
        out.append(f"> **Screening run**: at most {args.order_cap} buyers per session.  This "
                   "changes the statistics; do not compare with a full run.\n")
    out.append("## 1. Scenarios\n")
    out.append("| scenario | what it changes |\n|---|---|")
    out.extend(f"| `{name}` | {NOTES.get(name, '')} |" for name in variants)
    out.append("")
    out.append("## 2. Outcome\n")
    out.append(table(rows) + "\n")
    out.append("- **sell-through**: tickets held at the end / seats.  Cancelled and unpaid "
               "orders count only if their seats were sold again.\n"
               "- **first sell-out**: the first moment a session had no ticket left (a "
               "returned seat can put it back on sale).\n"
               "- **refused: stale**: the request failed because the cell held fewer tickets "
               "than the buyer had seen - the main way requests fail in a rush.\n")
    out.append("## 3. Platform load\n")
    out.append(traffic_table(rows) + "\n")
    out.append("- One request is one HTTP-like call by a buyer: a look at the inventory page, a "
               "purchase request, a poll for its answer, or a checkout.  The stages are counted, "
               "not weighted.\n"
               "- The polls are derived: every request waits `poll_seconds` for its answer and "
               "the client polls every `poll_interval` meanwhile.\n"
               "- **requests / s** is averaged over the whole simulated time; the load during the "
               "opening rush is far higher (see the load chart of the web UI).\n")
    out.append("## 4. Parameters of the baseline\n")
    out.append(constants_table(variants["baseline"]) + "\n")

    path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
