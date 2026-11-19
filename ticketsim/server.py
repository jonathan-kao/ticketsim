"""A small web server for the simulator (stdlib only, no dependencies).

    python3 -m ticketsim serve --port 8000

The API is deliberately tiny - the UI talks to the backend **only when the
user acts**:

======================  ====================================================
``GET  /``             the UI
``GET  /api/bootstrap``  presets + the parameter schema
``POST /api/run``        start a computation (the parameters go here once)
``GET  /api/job/<id>``   its status; the whole bundle comes back when it is done
======================  ====================================================

After the bundle arrives, the browser plays the timeline **locally**: playing,
pausing, seeking, zooming and editing the parameters never touch the network
again.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import __version__, scenarios as scenario_module
from .apischema import config_schema
from .live import Job
from .schema import Config

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class _State:
    """The jobs the server knows about (the newest one is the current run)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: Dict[str, Job] = {}
        self.current: Optional[str] = None

    def add(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job
            self.current = job.id
            # older jobs are not needed any more
            if len(self.jobs) > 8:
                for key in list(self.jobs)[:-8]:
                    self.jobs.pop(key, None)

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)


STATE = _State()


class Handler(BaseHTTPRequestHandler):
    server_version = "ticketsim"

    # -- helpers -------------------------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str, content_type: str) -> None:
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            self._send_json({"error": f"not found: {name}"}, 404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "invalid json"}

    def log_message(self, fmt: str, *args: Any) -> None:      # quieter console
        if os.environ.get("TICKETSIM_HTTP_LOG"):
            super().log_message(fmt, *args)

    # -- routes --------------------------------------------------------------------------
    def do_GET(self) -> None:                                  # noqa: N802
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
            return
        if route.path == "/api/bootstrap":
            self._send_json({
                "schema": config_schema(),
                "presets": {name: scenario_module.build(name).to_dict()
                            for name in scenario_module.names()},
                "preset_names": list(scenario_module.names()),
                "version": __version__,
            })
            return
        if route.path.startswith("/api/job/"):
            job = STATE.get(route.path.rsplit("/", 1)[-1])
            if job is None:
                self._send_json({"error": "unknown job"}, 404)
                return
            payload: Dict[str, Any] = {"status": job.status, "progress": job.progress}
            if job.error:
                payload["error"] = job.error
            if job.status == "done" and job.bundle is not None:
                payload["bundle"] = job.bundle
            self._send_json(payload)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:                                 # noqa: N802
        route = urlparse(self.path)
        body = self._body()
        if route.path == "/api/run":
            config_dict = body.get("config")
            try:
                config = (Config.from_dict(config_dict) if config_dict
                           else scenario_module.build(body.get("preset", "baseline")))
                config.validate()
            except Exception as exc:                            # noqa: BLE001
                self._send_json({"error": str(exc)}, 400)
                return
            job = Job(config)
            STATE.add(job)
            job.start()
            self._send_json({"job": job.id, "status": job.status})
            return
        self._send_json({"error": "not found"}, 404)


def serve(port: int = 8000, host: str = "127.0.0.1", open_browser: bool = False) -> int:
    """Run the web UI until interrupted."""
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ticketsim web UI → {url} (Ctrl-C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0
