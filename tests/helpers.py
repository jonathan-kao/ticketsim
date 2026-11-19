"""Shared helpers for the test suite."""
from __future__ import annotations

from ticketsim.schema import (BehaviorSpec, Config, ModelConstantsSpec, ScenarioSpec,
                              SessionSpec)


def small_config(buyers: int = 400, seats: int = 200, cap: int = 2, horizon: float = 300.0,
                 seed: int = 7, cells: int = 4, **behavior) -> Config:
    """A tiny but complete scenario: two sessions, real dynamics, fast to run."""
    sessions = (
        SessionSpec(key="s/a", name="A", seats=seats, per_buyer_limit=cap, open_at=0.0),
        SessionSpec(key="s/b", name="B", seats=seats, per_buyer_limit=cap, open_at=0.0),
    )
    return Config(
        scenario=ScenarioSpec(buyers=buyers, sessions=sessions, horizon=horizon),
        behavior=BehaviorSpec(**behavior),
        model_constants=ModelConstantsSpec(seed=seed, area_count=cells),
    )
