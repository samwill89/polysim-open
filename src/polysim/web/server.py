"""stdlib-only HTTP server for the dashboard.

Two endpoints:
  GET /                   → SPA HTML (single file)
  GET /api/state[?...]    → JSON snapshot from `collect_dashboard_state`

Optional query params on /api/state:
  flag=<id>     pin a flag in the detail panel
  run=<id>      pin a run in the position/P&L panels

The handler is sync (HTTPServer); for each /api/state we spin a fresh
asyncio loop to await the DAO calls. Cheap enough at the read-only
dashboard refresh cadence (~1Hz).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from polysim.web.state import collect_dashboard_state

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_index_html() -> str:
    return (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """One handler instance per request. db_path passed via the server attr."""

    server_version = "PolySimDashboard/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Stdlib's default writes to stderr — route through our logger so
        # the operator's terminal stays clean unless DEBUG is on.
        log.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            self._send_html(_load_index_html())
            return
        if url.path == "/api/state":
            self._serve_state(parse_qs(url.query))
            return
        if url.path == "/api/health":
            self._send_json({"ok": True})
            return
        self._send_status(404, "not found")

    def _serve_state(self, qs: dict[str, list[str]]) -> None:
        flag_id = _first_int(qs, "flag")
        run_id = _first_int(qs, "run")
        db_path: Path = self.server.db_path  # type: ignore[attr-defined]
        try:
            state = asyncio.run(
                collect_dashboard_state(
                    db_path,
                    selected_flag_id=flag_id,
                    selected_run_id=run_id,
                )
            )
        except Exception as exc:
            log.exception("collect_dashboard_state failed")
            self._send_json({"error": type(exc).__name__, "detail": str(exc)}, status=500)
            return
        self._send_json(state)

    # ── low-level senders ───────────────────────────────

    def _send_html(self, body: str, *, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_status(self, status: int, msg: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode("utf-8"))


def _first_int(qs: dict[str, list[str]], key: str) -> int | None:
    vals = qs.get(key)
    if not vals:
        return None
    try:
        return int(vals[0])
    except ValueError:
        return None


class DashboardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries the db_path."""

    def __init__(
        self, address: tuple[str, int], db_path: Path
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.db_path = db_path


def run_server(
    db_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    serve_forever: bool = True,
) -> DashboardServer:
    """Start a dashboard server. When `serve_forever=False`, returns the
    server immediately (caller is responsible for `serve_forever()` /
    `shutdown()`) — used by tests.
    """
    server = DashboardServer((host, port), db_path)
    if serve_forever:
        server.serve_forever()
    return server


def run_server_in_thread(
    db_path: Path, *, host: str = "127.0.0.1", port: int = 8765
) -> tuple[DashboardServer, threading.Thread]:
    """Convenience: spin the server up in a daemon thread; return both
    so the caller can call `server.shutdown()` to stop it cleanly."""
    server = run_server(db_path, host=host, port=port, serve_forever=False)
    thread = threading.Thread(
        target=server.serve_forever,
        name="dashboard-server",
        daemon=True,
    )
    thread.start()
    return server, thread
