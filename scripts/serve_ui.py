#!/usr/bin/env python3
"""Start the web UI in one step: start the backend, wait until it answers, open the page.

    python3 scripts/serve_ui.py                        # backend on :8000 + a browser
    python3 scripts/serve_ui.py --preset demand_60x    # open with a scenario loaded
    python3 scripts/serve_ui.py --port 8123
    python3 scripts/serve_ui.py --no-open              # only the backend

The page is served by the backend process itself (``GET /`` returns the page,
``/api/*`` returns JSON), so this script only does three things:

1. start the backend (``python3 -m ticketsim serve``) as a child process, so
   its log shows up here;
2. poll ``/api/bootstrap`` until the backend really answers (a live process
   is not enough);
3. open the browser at ``/?preset=...``.

Ctrl-C stops the backend too.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def port_is_free(port: int) -> bool:
    """Nobody listens on this port yet (do not start a second backend)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_ready(url: str, timeout: float) -> bool:
    """Wait until the backend answers the API (not only until the process lives)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="start the web UI (backend + page)")
    parser.add_argument("--port", type=int, default=8000, help="backend port (default 8000)")
    parser.add_argument("--preset", default=None,
                        help="the scenario the page loads (e.g. baseline, demand_120x)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="seconds to wait for the backend")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)

    if not port_is_free(args.port):
        print(f"port {args.port} is in use (stop that process or pick another --port)",
              file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", "ticketsim", "serve", "--port", str(args.port)]
    print("starting the backend:", " ".join(cmd), flush=True)
    # start_new_session: the backend gets its own process group, so a Ctrl-C in
    # the terminal only reaches this launcher, which then stops the backend
    # (otherwise the launcher could die and leave the backend running)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True)

    def cleanup() -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def on_term(_signum, _frame):        # SIGTERM: stop the same way as Ctrl-C
        raise KeyboardInterrupt

    base = f"http://127.0.0.1:{args.port}"
    try:
        # install the handlers explicitly: a background process of a
        # non-interactive shell starts with SIGINT ignored
        signal.signal(signal.SIGINT, on_term)
        signal.signal(signal.SIGTERM, on_term)
        if not wait_ready(base + "/api/bootstrap", args.timeout):
            print("the backend did not answer in time, giving up", file=sys.stderr, flush=True)
            return 1
        query = {"preset": args.preset} if args.preset else {}
        url = base + "/" + ("?" + urllib.parse.urlencode(query) if query else "")
        print(f"backend ready: {base}", flush=True)
        print(f"page: {url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        print("Ctrl-C to stop (the backend stops too)", flush=True)
        proc.wait()
    except KeyboardInterrupt:
        print("\nstopping the backend…", flush=True)
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
