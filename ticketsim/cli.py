"""Command line interface.

    python -m ticketsim scenarios                    # list the scenarios
    python -m ticketsim run --preset baseline        # one run
    python -m ticketsim batch --presets baseline,limit_4 --seeds 0-9
    python -m ticketsim constants --preset baseline  # the constant table of a scenario
    python -m ticketsim visualize --preset baseline  # a self-contained HTML report
    python -m ticketsim serve                        # the web UI
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Sequence

from . import scenarios as scenario_module
from .report import write as write_report
from .runner import compare, format_table, make_executor, run, write_batch
from .schema import Config

#: the columns of the batch table
BATCH_COLUMNS = ("sell_through", "first_sold_out_at", "buyer_failure_rate",
                 "buyer_stale_failures", "traffic_total", "requests_per_ticket")


def _seed_list(text: str) -> List[int]:
    """``0-9`` → [0..9]; ``1,3,5`` → [1,3,5]."""
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


def _config(args) -> Config:
    if getattr(args, "config", None):
        with open(args.config, "r", encoding="utf-8") as handle:
            config = Config.from_dict(json.load(handle))
    else:
        config = scenario_module.build(args.preset)
    if getattr(args, "seed", None) is not None:
        config = config.with_seed(args.seed)
    return config


def cmd_serve(args) -> int:
    from .server import serve

    return serve(port=args.port, host=args.host, open_browser=args.open)


def cmd_scenarios(args) -> int:
    print("scenarios:")
    for name in scenario_module.names():
        print(f"  {name:20s} {scenario_module.build(name).summary()}")
    return 0


def cmd_constants(args) -> int:
    config = _config(args)
    print(f"{config.summary()}\n")
    print(f"{'parameter':<30} {'value':<56} source")
    for name, value, source in config.constants_table():
        print(f"{name:<30} {value:<56} {source}")
    return 0


def cmd_visualize(args) -> int:
    config = _config(args)
    result = run(config, trace=True)
    write_report(result, args.out)
    print(config.summary())
    print(f"wrote {args.out}")
    return 0


def cmd_run(args) -> int:
    config = _config(args)
    result = run(config, trace=args.trace, order_cap=args.order_cap)
    print(config.summary())
    print()
    summary = result.summary
    for key in ("seats", "tickets_sold", "tickets_held", "sell_through", "stranded_stock",
                "first_sold_out_at", "refunded", "cleared", "buyer_attempts",
                "buyer_successes", "buyer_failures", "buyer_failure_rate",
                "buyer_stale_failures", "buyer_retries", "buyer_capped", "traffic_total",
                "requests_per_second", "requests_per_ticket", "conservation_ok",
                "rule_violations"):
        print(f"  {key:<26} {summary.get(key)}")
    print()
    for key, session in result.sessions.items():
        print(f"  {key}: sold_out_at={session['sold_out_at']} remaining={session['remaining']} "
              f"held={session['held']} refunded={session['refunded']} "
              f"cleared={session['cleared']} attempts={session['buyer_attempts']} "
              f"cells={session['cells']} ({session['cell_capacity_min']}-"
              f"{session['cell_capacity_max']} seats per cell)")
    if args.json:
        payload = {"config": config.to_dict(), "summary": summary, "sessions": result.sessions}
        if args.trace:
            payload["trace"] = result.trace
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    return 0


def cmd_batch(args) -> int:
    presets = [p.strip() for p in args.presets.split(",") if p.strip()]
    variants = {name: scenario_module.build(name) for name in presets}
    seeds = _seed_list(args.seeds)
    executor = make_executor(args.jobs)
    try:
        payload = compare(variants, seeds, order_cap=args.order_cap, executor=executor)
    finally:
        if executor is not None:
            executor.shutdown()
    print(f"seeds {seeds[0]}-{seeds[-1]} ({len(seeds)} runs per scenario)\n" if seeds else "")
    print(format_table(payload["aggregate"], BATCH_COLUMNS))
    if args.out:
        path = args.out if args.out.endswith(".json") else os.path.join(args.out, "batch.json")
        write_batch(path, payload)
        print(f"\nwrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticketsim",
                                     description="discrete event simulator of a large ticket sale")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="list the scenarios")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("constants", help="print the constant table of a scenario")
    p.add_argument("--preset", default="baseline")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--config", default=None, help="JSON configuration file")
    p.set_defaults(func=cmd_constants)

    p = sub.add_parser("run", help="run one scenario")
    p.add_argument("--preset", default="baseline")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--config", default=None, help="JSON configuration file")
    p.add_argument("--json", default=None, help="write the result as JSON")
    p.add_argument("--trace", action="store_true", help="keep the event trace")
    p.add_argument("--order-cap", type=int, default=None,
                   help="cap the buyers per session (coarse screening; changes the statistics)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("visualize", help="write a self-contained HTML report of one run")
    p.add_argument("--preset", default="baseline")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--config", default=None, help="JSON configuration file")
    p.add_argument("--out", default="ticketsim-report.html")
    p.set_defaults(func=cmd_visualize)

    p = sub.add_parser("batch", help="compare scenarios over several seeds")
    p.add_argument("--presets", default="baseline")
    p.add_argument("--seeds", default="0-9")
    p.add_argument("--out", default=None, help="output directory or .json file")
    p.add_argument("--order-cap", type=int, default=None,
                   help="cap the buyers per session (coarse screening; changes the statistics)")
    p.add_argument("--jobs", type=int, default=0,
                   help="worker processes for the seeds (0 = one per core, 1 = serial)")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("serve", help="run the web UI (parameters, playback, charts)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="open a browser window")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
