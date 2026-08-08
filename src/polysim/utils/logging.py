"""Structured logging — JSON to disk, rich to console.

Build plan §0.9. Never log secrets (tokens, API keys). See §7.4.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class _JsonFormatter(logging.Formatter):
    """One JSON object per log line, ready for jq / jsonl tooling."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Structured extras passed via logger.info("x", extra={"k": v})
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in _LOG_RECORD_RESERVED:
                continue
            payload[k] = v
        return json.dumps(payload, default=str)


_LOG_RECORD_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


def configure(
    level: str = "INFO",
    json_file: str | Path | None = "./logs/polysim.jsonl",
    rotate_bytes: int = 10 * 1024 * 1024,
    keep_files: int = 14,
) -> None:
    """Wire root logger. Console: plain. File: JSONL rotating."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any pre-existing handlers (tests, re-configure scenarios).
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(console)

    if json_file is not None:
        path = Path(json_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=rotate_bytes, backupCount=keep_files, encoding="utf-8"
        )
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Convenience — prefer `logging.getLogger(__name__)` in module code."""
    return logging.getLogger(name)
