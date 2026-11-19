"""ticketsim - a discrete event simulator of a large ticket sale.

A crowd of buyers arrives when a sale opens and competes for the seats of a
seating plan split into cells.  A purchase succeeds if and only if the cell
still has the tickets *when the platform answers the request*.  Orders that
are cancelled or never paid put their seats back on sale, so the stock is a
dynamic resource.

    from ticketsim import build, run

    result = run(build("baseline"))
    print(result.summary["sell_through"])
"""
from __future__ import annotations

from .engine import Engine, PythonEngine, RunResult
from .runner import run
from .scenarios import build, names
from .schema import (BehaviorSpec, BuyerSettleSpec, CellSpec, Config, ModelConstantsSpec,
                     ScenarioSpec, SessionSpec)

__all__ = [
    "BehaviorSpec", "BuyerSettleSpec", "CellSpec", "Config", "Engine", "ModelConstantsSpec",
    "PythonEngine", "RunResult", "ScenarioSpec", "SessionSpec", "build", "names", "run",
]

__version__ = "0.1.0"
