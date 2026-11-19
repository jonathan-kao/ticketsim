"""The summary of a run: the indicators every report, test and UI reads.

Both engines build their summary here (the Rust world core in
``rust_engine.py`` and the Python reference in ``engine.py``), so the
counting rules exist exactly once and the two cannot drift apart.

The inputs are duck typed: a session needs ``key``, ``spec``, ``cells``,
``sold_out_at``, ``pool`` (``total``, ``remaining_total``, ``check()``),
``held_tickets()`` and its statistics.  ``ticketsim.session.Session`` (Python
engine) and the snapshots of ``rust_engine.py`` both qualify.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .metrics import Metrics
from .schema import Config


def polls_per_attempt(config: Config) -> int:
    """How many times a client polls while one request waits for its answer."""
    poll_seconds = config.model_constants.poll_seconds
    interval = config.model_constants.poll_interval
    return 1 if interval <= 0 else max(1, int(round(poll_seconds / interval)))


def traffic_summary(m: Metrics, config: Config) -> Dict[str, Any]:
    """The platform load: every request the crowd sends, by stage, and the total.

    | stage     | what                                   | how it is counted              |
    |-----------|----------------------------------------|--------------------------------|
    | inventory | a look at the inventory page           | one per observation            |
    | submit    | a purchase request                     | one per attempt                |
    | poll      | waiting for the answer of a request    | attempts x polls_per_attempt   |
    | checkout  | paying for an order                    | one per order                  |

    The stages are **not weighted**: how expensive each request is for the
    platform is a separate question and does not belong in the count.
    """
    polls = polls_per_attempt(config)
    stages = {
        "inventory": m.buyer_inventory_reads,
        "submit": m.buyer_attempts,
        "poll": m.buyer_attempts * polls,
        "checkout": m.buyer_orders,
    }
    stages["total"] = sum(stages.values())
    horizon = max(1e-9, config.scenario.horizon)
    return {
        "stages": stages,
        "polls_per_attempt": polls,
        "requests_per_second": stages["total"] / horizon,
    }


def run_summary(m: Metrics, sessions: Mapping[str, Any], traffic: Dict[str, Any],
                order_cap: Optional[int]) -> Dict[str, Any]:
    """The headline indicators of one run (``RunResult.summary``)."""
    seats = sum(s.pool.total for s in sessions.values())
    stranded = {key: s.pool.remaining_total for key, s in sessions.items()}
    held = {key: s.held_tickets() for key, s in sessions.items()}
    sold_out = [s.sold_out_at for s in sessions.values() if s.sold_out_at is not None]
    return {
        "seats": seats,
        "tickets_sold": m.tickets_sold,
        "tickets_held": sum(held.values()),
        "held_per_session": held,
        "stranded_stock": sum(stranded.values()),
        "stranded_per_session": stranded,
        "sell_through": round(sum(held.values()) / seats, 4) if seats else 0.0,
        "sessions_sold_out": len(sold_out),
        "first_sold_out_at": round(min(sold_out), 3) if sold_out else None,
        "refunded": m.refunded_tickets,
        "cleared": m.cleared_tickets,

        # -- buyers ---------------------------------------------------------------------
        "buyer_attempts": m.buyer_attempts,
        "buyer_successes": m.buyer_successes,
        "buyer_failures": m.buyer_failures,
        "buyer_failure_rate": round(m.failure_rate, 4),
        "buyer_stale_failures": m.buyer_stale_failures,
        "buyer_cell_short_failures": m.buyer_cell_short_failures,
        "buyer_retries": m.buyer_retries,
        "buyer_orders": m.buyer_orders,
        "buyer_parked": m.buyer_parked,
        "buyer_wakes": m.buyer_wakes,
        "buyer_capped": m.buyer_capped,
        "buyer_fingerprint": m.buyer_fingerprint,

        # -- platform load --------------------------------------------------------------
        "traffic": traffic,
        "traffic_total": traffic["stages"]["total"],
        "requests_per_second": round(traffic["requests_per_second"], 3),
        "requests_per_ticket": (round(traffic["stages"]["total"] / m.tickets_sold, 3)
                                if m.tickets_sold else None),

        # -- self checks ----------------------------------------------------------------
        "conservation_ok": all(s.pool.check() for s in sessions.values()),
        "rule_violations": m.rule_violations,
        "order_cap": order_cap,
    }


def session_summary(session: Any) -> Dict[str, Any]:
    """One row of ``RunResult.sessions``."""
    capacities = [cell.capacity for cell in session.cells] or [0]
    return {
        "key": session.key,
        "name": session.spec.name,
        "seats": session.spec.seats,
        "per_buyer_limit": session.spec.per_buyer_limit,
        "sold_out_at": session.sold_out_at,
        "remaining": session.pool.remaining_total,
        "held": session.held_tickets(),
        "refunded": session.refunded_tickets,
        "cleared": session.cleared_tickets,
        "buyer_attempts": session.buyer_attempts,
        "buyer_successes": session.buyer_successes,
        "buyer_failures": session.buyer_failures,
        "stale_failures": session.stale_failures,
        "cell_short_failures": session.cell_short_failures,
        "cells": len(session.cells),
        "cell_capacity_min": min(capacities),
        "cell_capacity_max": max(capacities),
        "pool_ok": session.pool.check(),
    }
