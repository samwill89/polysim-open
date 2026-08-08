"""Web dashboard — companion to Telegram + TUI.

Single-page SPA mirroring `polysim-demo.html`, served by a stdlib
`http.server` instance. Five panels: live ingest tail, TUI mirror, flag
detail, telegram chat history, latest run report.

Read-only: no endpoint mutates state. All data is pulled live from the
SQLite DB on each `/api/state` poll. Listens on localhost only by
default — operator can rebind with `--host` (use 127.0.0.1 in production).

This module deliberately overrides spec §2's "no web UI" ban per direct
operator request — the §16 paper-only invariant remains intact.
"""

from polysim.web.server import DashboardHandler, run_server

__all__ = ["DashboardHandler", "run_server"]
