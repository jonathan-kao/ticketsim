"""A JSON description of the tunable parameters (used by the web UI).

The schema is written by hand rather than introspected, because the UI needs
more than the type: the label, the range and the group a field belongs to.
It mirrors ``docs/PARAMETERS.md``; ``tests/test_live.py`` checks that every
field of the configuration is in it and vice versa.

Every field description carries a ``name`` - that is the key the UI reads from
and writes back into the configuration dictionary.
"""
from __future__ import annotations

from typing import Any, Dict, List

#: a field description: {"name", "type", "label", "default", ...}
Field = Dict[str, Any]


def _num(name: str, kind: str, default: float, label: str, **extra: Any) -> Field:
    field: Field = {"name": name, "type": kind, "default": default, "label": label}
    field.update(extra)
    return field


def _select(name: str, default: str, label: str, options: List[str]) -> Field:
    return {"name": name, "type": "select", "default": default, "label": label,
            "options": options}


SESSION_FIELDS: List[Field] = [
    {"name": "key", "type": "text", "label": "Key"},
    {"name": "name", "type": "text", "label": "Name"},
    {"name": "seats", "type": "int", "label": "Seats", "min": 0},
    {"name": "per_buyer_limit", "type": "int", "label": "Limit per buyer", "min": 0},
    {"name": "open_at", "type": "float", "label": "Opens (s)", "min": 0},
    {"name": "close_at", "type": "float", "label": "Closes (s, 0 = never)", "min": 0},
]


def config_schema() -> Dict[str, Any]:
    """The parameter tree the web UI renders as a settings form."""
    return {
        "scenario": {
            "label": "Scenario",
            "fields": [
                _num("buyers", "int", 120000, "Buyers in the crowd", min=0, step=1000),
                _num("horizon", "float", 1800.0, "Simulated time (s)", min=1, step=60),
            ],
            "sessions": {"label": "Sessions", "fields": SESSION_FIELDS},
        },
        "behavior": {
            "label": "Buyers and platform rules",
            "fields": [
                _num("refund_prob", "float", 0.03, "Orders cancelled", min=0, max=1, step=0.01),
                _num("clear_prob", "float", 0.08, "Orders never paid", min=0, max=1, step=0.01),
                _num("payment_window", "float", 600.0, "Payment window (s)", min=0, step=60),
                _num("clear_period", "float", 720.0, "Returned seats every (s)", min=1, step=60),
                _num("hot_buyer_frac", "float", 0.30, "Buyers chasing popular cells",
                     min=0, max=1, step=0.05),
                _num("wake_budget_per_ticket", "float", 3.0, "Buyers woken per returned ticket",
                     min=1, step=1),
            ],
        },
        "model_constants": {
            "label": "Model constants",
            "fields": [
                _num("area_count", "int", 40, "Cells per seating plan", min=1),
                _select("area_capacity_shape", "zipf", "Cell size profile", ["zipf", "uniform"]),
                _num("area_capacity_spread", "float", 1.1, "Zipf exponent", min=0.1, step=0.1),
                _num("area_heat_weight", "float", 1.0, "Small cell = popular (weight)",
                     min=0, max=2, step=0.1),
                _num("poll_seconds", "float", 5.0, "Answer latency (s)", min=0, step=0.1),
                _num("poll_interval", "float", 0.5, "Poll interval (s)", min=0, step=0.1),
                _num("count_jitter", "float", 0.10, "Crowd size jitter", min=0, max=1, step=0.05),
                _select("crowd_impl", "synthetic", "Crowd implementation", ["synthetic", "object"]),
                _num("seed", "int", 20261106, "Seed", min=0),
            ],
        },
    }


def default_config_dict() -> Dict[str, Any]:
    """A configuration dictionary the UI can start from."""
    from . import scenarios

    return scenarios.build("baseline").to_dict()
