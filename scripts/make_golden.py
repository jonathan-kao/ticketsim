#!/usr/bin/env python3
"""Write or check the golden fixtures (the executable specification).

    python3 scripts/make_golden.py            # write tests/golden/*.json
    python3 scripts/make_golden.py --check    # only check (fail on a difference, write nothing)
    python3 scripts/make_golden.py --print    # also print a readable summary

Every fixture is the **complete event stream** of one scenario plus the pool
trajectory and the summary (see ``ticketsim/golden.py``).  The Rust core (or
any refactoring) must produce byte-identical output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ticketsim import golden                                        # noqa: E402


def readable_summary(fixture: dict) -> str:
    """A human readable summary of a fixture (the file itself is machine first)."""
    payload = fixture["payload"]
    summary = payload["summary"]
    counts: dict = {}
    for event in payload["trace"]:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    lines = [f"== {fixture['name']} (seed {fixture['seed']})",
             f"   events={len(payload['trace'])}  pool_history={len(payload['pool_history'])}",
             f"   sell_through={summary['sell_through']}  tickets_sold={summary['tickets_sold']}  "
             f"buyer_attempts={summary['buyer_attempts']}",
             f"   kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
             f"   sha256={fixture['sha256']}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate the golden fixtures")
    parser.add_argument("--check", action="store_true",
                        help="only check the fixtures; fail when they differ")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="print a readable summary")
    parser.add_argument("--out", default=golden.GOLDEN_DIR)
    args = parser.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    failures = []
    for name, config, seed in golden.CASES:
        fixture = golden.build_fixture(name, config, seed)
        path = os.path.join(args.out, f"{name}.json")
        if args.check:
            if not os.path.exists(path):
                failures.append(f"{name}: missing ({path})")
                continue
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if stored.get("sha256") != fixture["sha256"]:
                failures.append(f"{name}: differs (stored {stored.get('sha256')}, "
                                f"now {fixture['sha256']})")
                if args.show:
                    print(readable_summary(fixture))
            else:
                print(f"OK   {name}")
        else:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(fixture, handle, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
            print(f"wrote {path}")
            if args.show:
                print(readable_summary(fixture))
    if failures:
        for line in failures:
            print(f"FAIL {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
