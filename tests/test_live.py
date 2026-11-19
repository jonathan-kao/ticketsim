"""The bundle collector and the web API (the UI behind ``python3 -m ticketsim serve``).

The important property: the bundle is **the same simulation** as a batch run -
the UI only changes how the result is delivered (one transfer instead of a
stream), never the event order.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ticketsim.live import Collector, Job
from ticketsim.runner import run
from ticketsim.server import Handler

from helpers import small_config


class BundleTests(unittest.TestCase):
    def test_the_bundle_matches_a_batch_run(self):
        config = small_config(buyers=600, seats=150, horizon=600.0, clear_prob=0.3,
                              clear_period=150.0)
        bundle = Collector(config).run()
        batch = run(config)
        self.assertEqual(bundle.summary, batch.summary)
        self.assertTrue(batch.summary["conservation_ok"])

    def test_the_bundle_carries_every_stream_the_ui_needs(self):
        bundle = Collector(small_config(buyers=150, seats=120, horizon=120.0)).run()
        data = bundle.to_json()
        for key in ("config", "horizon", "duration", "polls_per_attempt", "samples",
                    "markers", "cells", "cell_labels", "sessions", "summary"):
            self.assertIn(key, data)
        self.assertTrue(data["samples"])
        self.assertTrue(data["cells"])
        self.assertEqual(set(data["cell_labels"]), {"s/a", "s/b"})
        json.dumps(data)                       # the bundle is plain JSON

    def test_the_samples_are_monotonic_and_complete(self):
        bundle = Collector(small_config(buyers=300, seats=100, horizon=200.0)).run()
        times = [s["t"] for s in bundle.samples]
        self.assertEqual(times, sorted(times))
        previous = None
        for sample in bundle.samples:
            self.assertEqual(sample["total"], sum(sample["rem"].values()))
            if previous is not None:
                for key in ("tickets", "attempts", "failures", "reads", "orders"):
                    self.assertGreaterEqual(sample[key], previous[key], key)
            previous = sample

    def test_the_cell_snapshots_are_the_true_stock(self):
        bundle = Collector(small_config(buyers=300, seats=100, horizon=200.0)).run()
        last = bundle.cells[-1]
        for key, remaining in last["sessions"].items():
            capacities = [cell[2] for cell in bundle.cell_labels[key]]
            self.assertEqual(len(remaining), len(capacities))
            for left, capacity in zip(remaining, capacities):
                self.assertTrue(0 <= left <= capacity)
            self.assertEqual(sum(remaining), bundle.samples[-1]["rem"][key])

    def test_the_markers_are_the_batches(self):
        config = small_config(buyers=600, seats=150, horizon=600.0, clear_prob=0.5,
                              clear_period=120.0, payment_window=60.0)
        bundle = Collector(config).run()
        batches = [m for m in bundle.markers if m["kind"] == "cleared"]
        self.assertTrue(batches)
        self.assertEqual(sum(m["qty"] for m in batches), bundle.summary["cleared"])

    def test_the_job_reports_progress_and_finishes(self):
        job = Job(small_config(buyers=100, seats=100, horizon=60.0))
        job.start()
        job._thread.join(timeout=60)
        self.assertEqual(job.status, "done")
        self.assertIsNotNone(job.bundle)
        self.assertEqual(job.progress, 1.0)


class SchemaTests(unittest.TestCase):
    def test_the_form_covers_every_parameter(self):
        """Every scalar field of the configuration is in the UI schema and vice versa."""
        from ticketsim.apischema import config_schema
        from ticketsim.schema import Config
        schema = config_schema()
        config = Config().to_dict()
        for group in ("scenario", "behavior", "model_constants"):
            form = {field["name"] for field in schema[group]["fields"]}
            scalars = {name for name, value in config[group].items()
                       if not isinstance(value, (list, dict))}
            self.assertEqual(form, scalars, group)
        sessions = {field["name"] for field in schema["scenario"]["sessions"]["fields"]}
        self.assertEqual(sessions, set(config["scenario"]["sessions"] and [] or
                                       ["key", "name", "seats", "per_buyer_limit", "open_at",
                                        "close_at"]))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return response.status, json.loads(response.read())

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())

    def test_bootstrap_lists_the_scenarios_and_the_schema(self):
        status, data = self._get("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("baseline", data["preset_names"])
        self.assertIn("scenario", data["schema"])
        # every field needs a name, or the UI cannot write the value back
        for group, spec in data["schema"].items():
            for field in spec.get("fields", []):
                self.assertIn("name", field, f"{group}.{field}")

    def test_run_returns_a_job_and_the_bundle_arrives(self):
        status, data = self._post("/api/run", {"preset": "baseline"})
        self.assertEqual(status, 200)
        self.assertIn("job", data)
        job_id = data["job"]
        bundle = None
        for _ in range(200):
            status, state = self._get(f"/api/job/{job_id}")
            if state["status"] == "done":
                bundle = state["bundle"]
                break
            if state["status"] == "error":
                self.fail(state.get("error"))
        self.assertIsNotNone(bundle, "the job should finish")
        self.assertTrue(bundle["samples"])
        self.assertTrue(bundle["markers"])
        self.assertEqual(bundle["summary"]["conservation_ok"], True)

    def test_a_broken_configuration_is_rejected(self):
        status = 0
        try:
            self._post("/api/run", {"config": {"scenario": {"buyers": -1}}})
        except urllib.error.HTTPError as error:
            status = error.code
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
