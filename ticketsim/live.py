"""Collect a whole run into one self-contained bundle.

The web UI does **not** stream the simulation: it sends the parameters once,
the backend runs the whole scenario, and the result comes back as one bundle
(the stock over time, the platform load, the batches of returned seats and
the stock of every cell).  The browser then plays that timeline back locally -
playing, pausing, seeking and zooming never touch the network again.

Why it matters: a run of this size takes well under a second in the Rust
core, and streaming it at a few Hz would have the browser re-render the whole
page on every poll.  One transfer and local playback avoid both.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .engine import Engine
from .schema import Config
from .summary import polls_per_attempt

#: simulated seconds between two snapshots of the cells
CELL_SNAPSHOT_INTERVAL = 2.0

#: simulated seconds between two samples of the pools and the counters
SAMPLE_INTERVAL = 0.25

#: the traced events the timeline marks
MARKED_EVENTS = ("cleared", "sale_open")


class Bundle:
    """Everything the UI needs to replay a run."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.samples: List[Dict[str, Any]] = []
        self.markers: List[Dict[str, Any]] = []
        self.cells: List[Dict[str, Any]] = []
        self.cell_labels: Dict[str, List[List[Any]]] = {}
        self.sessions: Dict[str, Any] = {}
        self.summary: Dict[str, Any] = {}

    def to_json(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "horizon": self.config.scenario.horizon,
            "duration": self.samples[-1]["t"] if self.samples else 0.0,
            "polls_per_attempt": polls_per_attempt(self.config),
            "samples": self.samples,
            "markers": self.markers,
            "cells": self.cells,
            "cell_labels": self.cell_labels,
            "sessions": self.sessions,
            "summary": self.summary,
        }


class Collector:
    """Runs a scenario and records everything the UI replays."""

    def __init__(self, config: Config, on_progress=None) -> None:
        self.config = config
        self.on_progress = on_progress
        self.engine = Engine(config, trace_enabled=False)
        self.bundle = Bundle(config)
        self._last_sample_t = -1.0
        self._last_cell_t = -1.0
        self.engine.observers.append(self._on_event)

    # -- run ------------------------------------------------------------------------------
    def run(self) -> Bundle:
        engine = self.engine
        engine.start()
        horizon = max(1e-9, self.config.scenario.horizon)
        while True:
            done = engine.advance(200)
            self._sample()
            self._snapshot_cells()
            if self.on_progress is not None:
                self.on_progress(min(1.0, engine.now / horizon))
            if done:
                break
        self._sample(force=True)
        self._snapshot_cells(force=True)
        result = engine._result
        self.bundle.summary = result.summary
        self.bundle.sessions = result.sessions
        return self.bundle

    # -- collection -----------------------------------------------------------------------
    def _on_event(self, item: Dict[str, Any]) -> None:
        if item.get("kind") in MARKED_EVENTS:
            self.bundle.markers.append(dict(item))

    def _sample(self, force: bool = False) -> None:
        now = self.engine.now
        if not force and now - self._last_sample_t < SAMPLE_INTERVAL:
            return
        self._last_sample_t = now
        m = self.engine.metrics
        rem = {key: session.pool.remaining_total
               for key, session in self.engine.sessions.items()}
        self.bundle.samples.append({
            "t": round(now, 3), "rem": rem, "total": sum(rem.values()),
            "tickets": m.tickets_sold, "attempts": m.buyer_attempts,
            "failures": m.buyer_failures, "reads": m.buyer_inventory_reads,
            "orders": m.buyer_orders,
        })

    def _snapshot_cells(self, force: bool = False) -> None:
        """The true stock of every cell (what the platform holds, not what anyone saw)."""
        now = self.engine.now
        if not force and now - self._last_cell_t < CELL_SNAPSHOT_INTERVAL:
            return
        self._last_cell_t = now
        snapshot: Dict[str, Any] = {"t": round(now, 3), "sessions": {}}
        for key, session in self.engine.sessions.items():
            if key not in self.bundle.cell_labels:
                self.bundle.cell_labels[key] = [[list(cell.key), cell.label, cell.capacity]
                                                for cell in session.cells]
            remaining = session.pool.remaining
            snapshot["sessions"][key] = [remaining.get(cell.key, 0) for cell in session.cells]
        self.bundle.cells.append(snapshot)


class Job:
    """A run in progress (the UI polls only *this* while it computes)."""

    _counter = 0
    _lock = threading.Lock()

    def __init__(self, config: Config) -> None:
        with Job._lock:
            Job._counter += 1
            self.id = f"job{Job._counter}"
        self.config = config
        self.status = "running"          # running | done | error
        self.progress = 0.0
        self.error: Optional[str] = None
        self.bundle: Optional[Dict[str, Any]] = None
        self._thread = threading.Thread(target=self._work, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(1.0, value))

    def _work(self) -> None:
        try:
            collector = Collector(self.config, on_progress=self._set_progress)
            bundle = collector.run()
            self.bundle = bundle.to_json()
            self.status = "done"
        except Exception as exc:                        # noqa: BLE001
            self.error = str(exc)
            self.status = "error"
        finally:
            self.progress = 1.0
