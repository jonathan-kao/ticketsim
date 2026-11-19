"""Single runs, seed batches and parameter comparisons.

The runs of a report are independent of each other (every run is a pure
function of the configuration and the seed), so they can be spread over
several processes.  ``compare(..., executor=...)`` does that: one worker per
seed, and every variant of that seed shares the crowd cache (variants that
only change the platform rules see exactly the same buyers).
"""
from __future__ import annotations

import json
import os
import statistics
from concurrent.futures import Executor, ProcessPoolExecutor
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .engine import Engine, RunResult
from .schema import Config

#: the summary fields a batch averages over the seeds
AGGREGATE_KEYS = (
    "tickets_sold", "tickets_held", "stranded_stock", "sell_through", "sessions_sold_out",
    "first_sold_out_at", "refunded", "cleared",
    "buyer_attempts", "buyer_successes", "buyer_failures", "buyer_failure_rate",
    "buyer_stale_failures", "buyer_cell_short_failures", "buyer_retries", "buyer_orders",
    "buyer_parked", "buyer_wakes", "buyer_capped",
    "traffic_total", "requests_per_second", "requests_per_ticket",
)


def run(config: Config, trace: bool = False, order_cap: Optional[int] = None,
        crowd_cache: Optional[Dict[Any, Any]] = None,
        backend: Optional[str] = None) -> RunResult:
    """Run one scenario.

    ``backend`` picks the engine (``"rust"``, the default, or ``"python"``,
    the reference implementation); the result does not depend on it.

    ``order_cap`` limits the number of buyers per session.  It is a coarse
    screening device: it changes the statistics and must be reported.

    ``crowd_cache`` reuses the crowd between runs that share the seed and the
    crowd parameters - building it is ~10 % of a run.
    """
    engine = Engine(config, trace_enabled=trace, order_cap=order_cap,
                    crowd_cache=crowd_cache, backend=backend)
    return engine.run()


def make_executor(jobs: int = 0) -> Optional[ProcessPoolExecutor]:
    """A process pool for a report (``jobs=0`` → one worker per core).

    ``None`` means "run in this process" (the serial path).
    """
    if jobs <= 0:
        jobs = os.cpu_count() or 1
    if jobs <= 1:
        return None
    return ProcessPoolExecutor(max_workers=jobs)


def _run_one_seed(payload: Tuple[List[Tuple[str, Config]], int, Optional[int]]
                  ) -> List[Tuple[str, Dict[str, Any]]]:
    """Run every variant of one seed (the worker of the parallel path)."""
    variants, seed, order_cap = payload
    crowd_cache: Dict[Any, Any] = {}
    return [(label, run(config.with_seed(seed), order_cap=order_cap,
                        crowd_cache=crowd_cache).summary)
            for label, config in variants]


def submit_seed(executor: Executor, variants: Mapping[str, Config], seed: int,
                order_cap: Optional[int] = None):
    """Submit one seed (all its variants) to a pool.

    A report submits *every* seed of *every* scan at once, so that all the
    cores stay busy - the scenarios are of very different sizes.
    """
    return executor.submit(_run_one_seed, (list(variants.items()), seed, order_cap))


def compare(variants: Mapping[str, Config], seeds: Sequence[int],
            order_cap: Optional[int] = None,
            executor: Optional[Executor] = None) -> Dict[str, Any]:
    """Run every variant on every seed and average the headline indicators.

    The same seed means the same crowd draws, so the variants are compared
    pairwise.  With an ``executor`` the seeds run in parallel; the result does
    not depend on it.
    """
    results: Dict[str, List[Dict[str, Any]]] = {label: [] for label in variants}
    if executor is None:
        for seed in seeds:
            for label, summary in _run_one_seed((list(variants.items()), seed, order_cap)):
                results[label].append(summary)
    else:
        futures = [submit_seed(executor, variants, seed, order_cap) for seed in seeds]
        for future in futures:
            for label, summary in future.result():
                results[label].append(summary)
    return {
        "seeds": list(seeds),
        "variants": {label: config.to_dict() for label, config in variants.items()},
        "order_cap": order_cap,
        "runs": results,
        "aggregate": {label: aggregate(rows) for label, rows in results.items()},
    }


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Mean / standard deviation of the headline indicators over the seeds."""
    if not rows:
        return {}
    out: Dict[str, Any] = {"n": len(rows)}
    flat = [dict(row, **{f"traffic_{stage}": value
                         for stage, value in row.get("traffic", {}).get("stages", {}).items()})
            for row in rows]
    stages = [f"traffic_{stage}" for stage in rows[0].get("traffic", {}).get("stages", {})]
    for key in list(AGGREGATE_KEYS) + [s for s in stages if s not in AGGREGATE_KEYS]:
        values = [row[key] for row in flat if isinstance(row.get(key), (int, float))
                  and not isinstance(row.get(key), bool)]
        if not values:
            continue
        out[key] = round(statistics.fmean(values), 4)
        if len(values) > 1:
            out[key + "_sd"] = round(statistics.pstdev(values), 4)
    return out


def write_batch(path: str, payload: Dict[str, Any]) -> None:
    """Write a batch result as JSON plus a flat CSV of the averages."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    rows = payload.get("aggregate", {})
    if not rows:
        return
    keys = sorted({k for row in rows.values() for k in row if not k.endswith("_sd")})
    with open(os.path.splitext(path)[0] + ".csv", "w", encoding="utf-8") as handle:
        handle.write("variant," + ",".join(keys) + "\n")
        for label, row in rows.items():
            handle.write(label + "," + ",".join(str(row.get(k, "")) for k in keys) + "\n")


def format_table(rows: Dict[str, Dict[str, Any]], columns: Sequence[str]) -> str:
    """A small fixed-width table (used by the CLI)."""
    width = max([len("variant")] + [len(name) for name in rows]) + 2
    lines = [f"{'variant':<{width}}" + "".join(f"{c:>20}" for c in columns)]
    for name, row in rows.items():
        cells = []
        for column in columns:
            value = row.get(column, "")
            cells.append(f"{value:>20.3f}" if isinstance(value, float) else f"{str(value):>20}")
        lines.append(f"{name:<{width}}" + "".join(cells))
    return "\n".join(lines)
