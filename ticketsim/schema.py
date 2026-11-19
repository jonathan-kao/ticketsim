"""The parameter model of a run.

Every field has a default and a documented meaning (docs/PARAMETERS.md), and
:meth:`Config.constants_table` renders the values in force with the source of
each one, so a report can always be reproduced exactly.

Design notes
------------
* The schema is a plain frozen-dataclass tree: no validation framework, no
  dynamic typing.  ``Config.from_dict`` / ``Config.to_dict`` are the only
  serialisation entry points (JSON in, JSON out).
* All defaults are synthetic, chosen to give a plausible large on-sale
  (a crowd several times larger than the stock).  None of them is measured
  on a real platform; docs/PARAMETERS.md says which ones the results are
  sensitive to.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .rng import channel_seed

# --------------------------------------------------------------------------------------
# model constants
# --------------------------------------------------------------------------------------

#: Buyer speed mixture: (tier, share, median delay s, log-normal sigma, floor s).
#:
#: The delay is the time a buyer needs from looking at the inventory page to
#: submitting a request, and also the interval between two attempts.  Each
#: tier has a hard floor: nobody is faster than the floor however lucky the
#: draw, so the left tail of the distribution is not a hidden parameter.
#: The shares and medians are a synthetic assumption: a small automated
#: minority, some fast people, and a large majority of ordinary buyers.
BUYER_MIX: Tuple[Tuple[str, float, float, float, float], ...] = (
    ("fast", 0.005, 0.80, 0.30, 0.50),
    ("mid", 0.05, 1.80, 0.50, 1.20),
    ("slow", 0.945, 4.50, 1.00, 2.50),
)

#: Order size distribution (1-4 tickets, mean 1.40) - synthetic assumption.
BUYER_QTY_WEIGHTS: Tuple[float, ...] = (0.74, 0.16, 0.06, 0.04)

#: A seating cell: one area of the seating plan (optionally one ticket type).
#: The key is ``(area_id, ticket_type)``; a single-ticket-type session always
#: uses ``ticket_type = 0``.
CellKey = Tuple[int, int]


@dataclass(frozen=True)
class CellSpec:
    """One seating cell as written by hand (``SessionSpec.cells``).

    ``None`` means "derive it": the label becomes ``Zone-NN``, the price stays
    unknown and the heat is derived from the capacity.
    """

    label: str = ""
    capacity: int = 0
    price: Optional[int] = None
    heat: Optional[float] = None
    ticket_type: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "capacity": self.capacity, "price": self.price,
                "heat": self.heat, "ticket_type": self.ticket_type}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CellSpec":
        price = d.get("price")
        heat = d.get("heat")
        return cls(
            label=str(d.get("label", "")),
            capacity=int(d.get("capacity", 0)),
            price=None if price is None else int(price),
            heat=None if heat is None else float(heat),
            ticket_type=int(d.get("ticket_type", 0)),
        )


@dataclass(frozen=True)
class ResolvedCell:
    """A cell after normalisation - the single truth the engine works with.

    Both the automatic split (one integer, ``area_count``) and the hand written
    list (``SessionSpec.cells``) end up here, so nothing downstream has to know
    where a cell came from.
    """

    key: CellKey
    label: str
    capacity: int
    price: Optional[int]
    heat: float


def _auto_capacities(seats: int, n: int, shape: str, spread: float, seed: int,
                      session_key: str) -> List[int]:
    """Split ``seats`` over ``n`` cells (deterministic, exact sum).

    ``uniform`` gives every cell the same size; ``zipf`` gives a few big cells
    and many small ones.  The weights are shuffled with a session-keyed seed so
    the capacity profile is not monotone in the cell index.  The remainder of
    the division goes to the cells with the largest fractional part (ties: the
    smaller index).
    """
    if n <= 0:
        return []
    if shape == "uniform":
        weights = [1.0] * n
    else:
        weights = [1.0 / ((i + 1) ** spread) for i in range(n)]
    order = list(range(n))
    random.Random(channel_seed(seed, f"areas:{session_key}")).shuffle(order)
    shuffled = [0.0] * n
    for position, cell in enumerate(order):
        shuffled[cell] = weights[position]
    total = sum(shuffled) or 1.0
    raw = [seats * w / total for w in shuffled]
    base = [int(math.floor(x)) for x in raw]
    remainder = seats - sum(base)
    ranked = sorted(range(n), key=lambda i: (-(raw[i] - base[i]), i))
    for i in range(remainder):
        base[ranked[i % n]] += 1
    return base


def resolve_cells(spec: "SessionSpec", mc: "ModelConstantsSpec", seed: int) -> Tuple[ResolvedCell, ...]:
    """Normalise a session's seating plan into the cells the engine uses.

    Automatic mode (no ``SessionSpec.cells``) splits the seats over
    ``mc.area_count`` cells; manual mode uses the given list.  Missing fields
    are filled with the same rules in both modes, so the two paths cannot drift
    apart (docs/MODEL.md, "Seating plan").
    """
    if spec.cells:
        raw = list(spec.cells)
        capacities = [c.capacity for c in raw]
        total = sum(capacities)
        if spec.seats and spec.seats != total:
            raise ValueError(
                f"session {spec.key}: seats ({spec.seats}) must equal the sum of the "
                f"cell capacities ({total}) - or set seats=0 to derive it")
    else:
        count = max(1, mc.area_count)
        capacities = _auto_capacities(spec.seats, count, mc.area_capacity_shape,
                                      mc.area_capacity_spread, seed, spec.key)
        raw = [CellSpec(label=f"Zone-{i + 1:02d}", capacity=cap)
               for i, cap in enumerate(capacities)]
    biggest = max(capacities) if capacities else 1
    biggest = biggest or 1
    cells: List[ResolvedCell] = []
    for index, cell in enumerate(raw):
        heat = cell.heat
        if heat is None:
            heat = mc.area_heat_weight * (1.0 - cell.capacity / biggest)
        cells.append(ResolvedCell(key=(index, cell.ticket_type),
                                   label=cell.label or f"Zone-{index + 1:02d}",
                                   capacity=cell.capacity,
                                   price=cell.price,
                                   heat=float(heat)))
    return tuple(cells)



@dataclass(frozen=True)
class SessionSpec:
    """One event on sale."""

    key: str
    name: str = ""
    seats: int = 0                     # initial stock
    per_buyer_limit: int = 2           # tickets one buyer may buy in this session, in total
    open_at: float = 0.0
    close_at: float = 0.0              # 0 = sell until the horizon
    cells: Optional[Tuple[CellSpec, ...]] = None   # hand written seating plan

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "seats": self.seats,
            "per_buyer_limit": self.per_buyer_limit,
            "open_at": self.open_at, "close_at": self.close_at,
            "cells": None if self.cells is None else [c.to_dict() for c in self.cells],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SessionSpec":
        cells = d.get("cells")
        return cls(
            key=str(d["key"]),
            name=str(d.get("name", d["key"])),
            seats=int(d.get("seats", 0)),
            per_buyer_limit=int(d.get("per_buyer_limit", 2)),
            open_at=float(d.get("open_at", 0.0)),
            close_at=float(d.get("close_at", 0.0)),
            cells=None if cells is None else tuple(CellSpec.from_dict(c) for c in cells),
        )


@dataclass(frozen=True)
class ScenarioSpec:
    """What the world looks like: the crowd, the sessions, how long we watch."""

    buyers: int = 120000               # crowd size, shared out over the sessions by stock
    sessions: Tuple[SessionSpec, ...] = ()
    horizon: float = 1800.0            # seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buyers": self.buyers,
            "sessions": [s.to_dict() for s in self.sessions],
            "horizon": self.horizon,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ScenarioSpec":
        return cls(
            buyers=int(d.get("buyers", 120000)),
            sessions=tuple(SessionSpec.from_dict(s) for s in d.get("sessions", ())),
            horizon=float(d.get("horizon", 1800.0)),
        )


@dataclass(frozen=True)
class BuyerSettleSpec:
    """Distribution of the moment a buyer decides what to do with an order.

    ``deadline_weighted``: 30 % U(0,120) + 70 % U(360,600) - early deciders
    plus a wave just before the payment deadline.  ``uniform``: U(0,600),
    the control group.
    """

    kind: str = "deadline_weighted"
    early_frac: float = 0.30
    early_range: Tuple[float, float] = (0.0, 120.0)
    late_range: Tuple[float, float] = (360.0, 600.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "early_frac": self.early_frac,
            "early_range": list(self.early_range), "late_range": list(self.late_range),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BuyerSettleSpec":
        return cls(
            kind=str(d.get("kind", "deadline_weighted")),
            early_frac=float(d.get("early_frac", 0.30)),
            early_range=tuple(d.get("early_range", (0.0, 120.0))),  # type: ignore[arg-type]
            late_range=tuple(d.get("late_range", (360.0, 600.0))),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class BehaviorSpec:
    """How buyers behave and the platform rules that react to it."""

    refund_prob: float = 0.03          # share of orders cancelled (seats return at once)
    payment_window: float = 600.0      # seconds an order may stay unpaid
    clear_prob: float = 0.08           # share of orders never paid (seats return in a batch)
    clear_period: float = 720.0        # seconds between two batches of returned seats
    hot_buyer_frac: float = 0.30       # share of buyers who chase the most popular cells
    wake_budget_per_ticket: float = 3.0  # waiting buyers woken per returned ticket
    buyer_settle: BuyerSettleSpec = field(default_factory=BuyerSettleSpec)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refund_prob": self.refund_prob,
            "payment_window": self.payment_window, "clear_prob": self.clear_prob,
            "clear_period": self.clear_period,
            "hot_buyer_frac": self.hot_buyer_frac,
            "wake_budget_per_ticket": self.wake_budget_per_ticket,
            "buyer_settle": self.buyer_settle.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BehaviorSpec":
        return cls(
            refund_prob=float(d.get("refund_prob", 0.03)),
            payment_window=float(d.get("payment_window", 600.0)),
            clear_prob=float(d.get("clear_prob", 0.08)),
            clear_period=float(d.get("clear_period", 720.0)),
            hot_buyer_frac=float(d.get("hot_buyer_frac", 0.30)),
            wake_budget_per_ticket=float(d.get("wake_budget_per_ticket", 3.0)),
            buyer_settle=BuyerSettleSpec.from_dict(d.get("buyer_settle", {})),
        )


@dataclass(frozen=True)
class ModelConstantsSpec:
    """Model constants: the crowd, the seating plan, the platform's latency."""

    buyer_mix: Tuple[Tuple[str, float, float, float, float], ...] = BUYER_MIX
    buyer_qty_weights: Tuple[float, ...] = BUYER_QTY_WEIGHTS
    poll_seconds: float = 5.0            # submit -> answer latency of the platform
    area_count: int = 40                 # how many cells an automatic seating plan has
    area_capacity_shape: str = "zipf"    # capacity profile ("zipf" / "uniform")
    area_capacity_spread: float = 1.1    # zipf exponent (ignored for "uniform")
    area_heat_weight: float = 1.0        # how strongly small == popular
    count_jitter: float = 0.10           # +-10 % on the realised buyer count
    crowd_impl: str = "synthetic"        # "synthetic" (parallel arrays) or "object" (one per buyer)
    seed: int = 20261106
    #: Seconds between two polls of a waiting request.  The answer comes
    #: ``poll_seconds`` after the submit, and a client polls every
    #: ``poll_interval`` meanwhile, so one attempt costs
    #: ``poll_seconds / poll_interval`` polls (10 by default).  Set it equal to
    #: ``poll_seconds`` to count a single poll per attempt.
    poll_interval: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buyer_mix": [list(m) for m in self.buyer_mix],
            "buyer_qty_weights": list(self.buyer_qty_weights),
            "poll_seconds": self.poll_seconds,
            "area_count": self.area_count,
            "area_capacity_shape": self.area_capacity_shape,
            "area_capacity_spread": self.area_capacity_spread,
            "area_heat_weight": self.area_heat_weight,
            "count_jitter": self.count_jitter,
            "crowd_impl": self.crowd_impl,
            "seed": self.seed,
            "poll_interval": self.poll_interval,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ModelConstantsSpec":
        mix = d.get("buyer_mix")
        return cls(
            buyer_mix=tuple(tuple(m) for m in mix) if mix else BUYER_MIX,  # type: ignore[arg-type]
            buyer_qty_weights=tuple(d.get("buyer_qty_weights", BUYER_QTY_WEIGHTS)),  # type: ignore[arg-type]
            poll_seconds=float(d.get("poll_seconds", 5.0)),
            area_count=int(d.get("area_count", 40)),
            area_capacity_shape=str(d.get("area_capacity_shape", "zipf")),
            area_capacity_spread=float(d.get("area_capacity_spread", 1.1)),
            area_heat_weight=float(d.get("area_heat_weight", 1.0)),
            count_jitter=float(d.get("count_jitter", 0.10)),
            crowd_impl=str(d.get("crowd_impl", "synthetic")),
            seed=int(d.get("seed", 20261106)),
            poll_interval=float(d.get("poll_interval", 0.5)),
        )


@dataclass(frozen=True)
class Config:
    """The complete parameter set of one run."""

    scenario: ScenarioSpec = field(default_factory=ScenarioSpec)
    behavior: BehaviorSpec = field(default_factory=BehaviorSpec)
    model_constants: ModelConstantsSpec = field(default_factory=ModelConstantsSpec)

    # -- serialisation -------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario.to_dict(),
            "behavior": self.behavior.to_dict(),
            "model_constants": self.model_constants.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Config":
        return cls(
            scenario=ScenarioSpec.from_dict(d.get("scenario", {})),
            behavior=BehaviorSpec.from_dict(d.get("behavior", {})),
            model_constants=ModelConstantsSpec.from_dict(d.get("model_constants", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> "Config":
        return cls.from_dict(json.loads(text))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    # -- convenience ---------------------------------------------------------------------
    def with_seed(self, seed: int) -> "Config":
        return replace(self, model_constants=replace(self.model_constants, seed=seed))

    def with_behavior(self, behavior: Optional[BehaviorSpec] = None, **changes: Any) -> "Config":
        if behavior is not None:
            return replace(self, behavior=behavior)
        return replace(self, behavior=replace(self.behavior, **changes))

    def with_scenario(self, scenario: Optional[ScenarioSpec] = None, **changes: Any) -> "Config":
        if scenario is not None:
            return replace(self, scenario=scenario)
        return replace(self, scenario=replace(self.scenario, **changes))

    def with_constants(self, constants: Optional[ModelConstantsSpec] = None,
                       **changes: Any) -> "Config":
        if constants is not None:
            return replace(self, model_constants=constants)
        return replace(self, model_constants=replace(self.model_constants, **changes))

    def with_sessions(self, **changes: Any) -> "Config":
        """Change a field of every session (``with_sessions(per_buyer_limit=4)``)."""
        return self.with_scenario(sessions=tuple(replace(s, **changes)
                                                 for s in self.scenario.sessions))

    def validate(self) -> None:
        """Raise ``ValueError`` on an inconsistent configuration."""
        sc = self.scenario
        if sc.buyers < 0:
            raise ValueError("scenario.buyers must be >= 0")
        if not sc.sessions:
            raise ValueError("scenario.sessions must not be empty")
        keys = [s.key for s in sc.sessions]
        if len(set(keys)) != len(keys):
            raise ValueError("session keys must be unique")
        for s in sc.sessions:
            if s.seats < 0:
                raise ValueError(f"session {s.key}: seats must be >= 0")
            if s.per_buyer_limit < 0:
                raise ValueError(f"session {s.key}: per_buyer_limit must be >= 0")
            if s.close_at and s.close_at <= s.open_at:
                raise ValueError(f"session {s.key}: close_at must be after open_at")
            if s.cells:
                total = sum(c.capacity for c in s.cells)
                if any(c.capacity < 0 for c in s.cells):
                    raise ValueError(f"session {s.key}: cell capacities must be >= 0")
                if s.seats and s.seats != total:
                    raise ValueError(
                        f"session {s.key}: seats ({s.seats}) must equal the sum of the cell "
                        f"capacities ({total}) - or set seats=0 to derive it")
        if sc.horizon <= 0:
            raise ValueError("scenario.horizon must be > 0")
        for name in ("refund_prob", "clear_prob", "hot_buyer_frac"):
            if not 0.0 <= getattr(self.behavior, name) <= 1.0:
                raise ValueError(f"behavior.{name} must be within [0, 1]")
        if not 0.0 <= self.model_constants.count_jitter <= 1.0:
            raise ValueError("model_constants.count_jitter must be within [0, 1]")
        if self.behavior.clear_period <= 0:
            raise ValueError("behavior.clear_period must be > 0")
        if self.model_constants.area_count < 1:
            raise ValueError("model_constants.area_count must be >= 1")
        if self.model_constants.area_capacity_shape not in ("zipf", "uniform"):
            raise ValueError("model_constants.area_capacity_shape must be 'zipf' or 'uniform'")
        if self.model_constants.area_capacity_spread <= 0:
            raise ValueError("model_constants.area_capacity_spread must be > 0")

    # -- reporting -----------------------------------------------------------------------
    def summary(self) -> str:
        """One-line summary of the values in force."""
        sc, bh, mc = self.scenario, self.behavior, self.model_constants
        seats = sum(s.seats for s in sc.sessions)
        caps = "/".join(str(s.per_buyer_limit) for s in sc.sessions)
        return (
            f"N={sc.buyers:,} · {len(sc.sessions)} sessions, {seats:,} seats · limit {caps} · "
            f"refund={bh.refund_prob:.0%} unpaid={bh.clear_prob:.0%} · "
            f"batch every {bh.clear_period:.0f}s · seed={mc.seed}"
        )

    def order_size_summary(self) -> str:
        """The effective order size distribution per purchase limit.

        The distribution is the size of a **whole order**; a single request is
        additionally limited by the cell it targets and by the buyer's
        remaining quota.
        """
        parts = []
        for cap in sorted({s.per_buyer_limit for s in self.scenario.sessions}):
            weights = self.model_constants.buyer_qty_weights[:cap]
            total = sum(weights) or 1.0
            mean = sum((i + 1) * w for i, w in enumerate(weights)) / total
            parts.append(f"limit {cap}: 1-{len(weights)} tickets, mean {mean:.2f}")
        return f"{'; '.join(parts)} (w={list(self.model_constants.buyer_qty_weights)})"

    def resolved_cells(self) -> Dict[str, Tuple[ResolvedCell, ...]]:
        """The seating plan of every session, after normalisation."""
        return {s.key: resolve_cells(s, self.model_constants, self.model_constants.seed)
                for s in self.scenario.sessions}

    def constants_table(self) -> List[Tuple[str, str, str]]:
        """``(name, value, source)`` rows - the constant table a report needs."""
        sc, bh, mc = self.scenario, self.behavior, self.model_constants
        cells = self.resolved_cells()
        return [
            ("buyers N", f"{sc.buyers}", "scenario"),
            ("sessions", ", ".join(f"{s.key}:{s.seats}" for s in sc.sessions), "scenario"),
            ("cells", ", ".join(f"{s.key}:{len(cells[s.key])}{' (manual)' if s.cells else ''}"
                                for s in sc.sessions),
             "automatic split (area_count) or hand written"),
            ("purchase limit", ", ".join(f"{s.key}:{s.per_buyer_limit}" for s in sc.sessions),
             "scenario (platform rule)"),
            ("horizon", f"{sc.horizon}s", "scenario"),
            ("refund_prob", f"{bh.refund_prob}", "synthetic assumption"),
            ("clear_prob / clear_period", f"{bh.clear_prob} / {bh.clear_period}s",
             "synthetic assumption (platform rule)"),
            ("payment_window", f"{bh.payment_window}s", "synthetic assumption (platform rule)"),
            ("decision time distribution", bh.buyer_settle.kind, "synthetic assumption"),
            ("buyer retries", "unlimited, interval = the buyer's own speed", "design decision"),
            ("hot buyer share", f"{bh.hot_buyer_frac:.0%}", "synthetic assumption"),
            ("wake budget per ticket", f"{bh.wake_budget_per_ticket}", "design decision"),
            ("buyer speed mix",
             ", ".join(f"{m[0]} {m[1]:.2%}/{m[2]}s(±{m[3]}, ≥{m[4]}s)" for m in mc.buyer_mix),
             "synthetic assumption"),
            ("order size", self.order_size_summary(), "synthetic assumption"),
            ("buyer arrival", "the whole crowd at the opening", "design decision"),
            ("poll_seconds / poll_interval", f"{mc.poll_seconds}s / {mc.poll_interval}s",
             "synthetic assumption"),
            ("cells (area_count)", f"{mc.area_count}", "synthetic assumption"),
            ("capacity shape / spread", f"{mc.area_capacity_shape} / {mc.area_capacity_spread}",
             "synthetic assumption"),
            ("heat weight", f"{mc.area_heat_weight}", "synthetic assumption"),
            ("count_jitter", f"±{mc.count_jitter:.0%}", "synthetic assumption"),
            ("crowd implementation", mc.crowd_impl, "parallel arrays / one object per buyer"),
            ("seed", f"{mc.seed}", "reproducibility"),
        ]
