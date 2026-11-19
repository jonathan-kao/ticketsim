"""Research scenarios.

``baseline`` is the reference world: two large events on sale at the same
time, a crowd three times the stock, a purchase limit of 2 per buyer.  The
other scenarios change one thing at a time - the demand, or one platform
rule - so the effect of that one thing can be read off the report.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, Tuple

from .schema import BehaviorSpec, BuyerSettleSpec, Config, ModelConstantsSpec, ScenarioSpec, SessionSpec

ARENA = SessionSpec(key="arena", name="Arena", seats=20000, per_buyer_limit=2, open_at=0.0)
STADIUM = SessionSpec(key="stadium", name="Stadium", seats=22500, per_buyer_limit=2, open_at=0.0)

#: default demand: buyers per ticket on sale
DEFAULT_RATIO = 3.0


def two_venues(ratio: float = DEFAULT_RATIO, cap: int = 2, horizon: float = 1800.0,
               seed: int = 20261106, venue_scale: float = 1.0) -> Config:
    """Two large events on sale at the same time.

    ``venue_scale`` shrinks the venues while keeping the demand ratio
    (buyers / seats) intact: the same pressure at a fraction of the cost.
    The demand scan uses it, because there the *ratio* is the variable.
    """
    sessions = (replace(ARENA, per_buyer_limit=cap, seats=int(ARENA.seats * venue_scale)),
                replace(STADIUM, per_buyer_limit=cap, seats=int(STADIUM.seats * venue_scale)))
    buyers = int(round(ratio * sum(s.seats for s in sessions)))
    return Config(
        scenario=ScenarioSpec(buyers=buyers, sessions=sessions, horizon=horizon),
        behavior=BehaviorSpec(),
        model_constants=ModelConstantsSpec(seed=seed),
    )


def _scenarios() -> Dict[str, Config]:
    out: Dict[str, Config] = {}
    out["baseline"] = two_venues()
    # demand: buyers per ticket.  The venues are scaled down by ten so that the
    # same ratio costs a tenth: 120x of 4,250 seats is 510,000 buyers.
    for ratio in (20.0, 60.0, 120.0):
        out[f"demand_{int(ratio)}x"] = two_venues(ratio=ratio, venue_scale=0.1)
    # the purchase limit per buyer and session
    for cap in (1, 4):
        out[f"limit_{cap}"] = two_venues(cap=cap)
    # how long an order may stay unpaid
    for window in (300.0, 900.0):
        out[f"payment_window_{int(window)}"] = two_venues().with_behavior(payment_window=window)
    # how often the seats of expired orders go back on sale
    for period in (240.0, 1440.0):
        out[f"batch_every_{int(period)}"] = two_venues().with_behavior(clear_period=period)
    # the seating plan: one big cell, or many small ones
    for count in (1, 200):
        out[f"cells_{count}"] = two_venues().with_constants(area_count=count)
    # when buyers decide what to do with an order
    out["uniform_decisions"] = two_venues().with_behavior(
        buyer_settle=replace(BuyerSettleSpec(), kind="uniform"))
    return out


def build(name: str) -> Config:
    """Return the configuration of the named scenario."""
    scenarios = _scenarios()
    if name not in scenarios:
        raise KeyError(f"unknown scenario {name!r}; available: {sorted(scenarios)}")
    return scenarios[name]


def names() -> Tuple[str, ...]:
    return tuple(_scenarios())
