"""Structured JSON logs that Cloud Logging understands. Never logs visitor IP addresses."""

import json
import logging
import os
import sys

_logger = logging.getLogger("vallum.app")
_quiet = os.environ.get("APP_LOG_REQUESTS", "1") == "0"


def setup():
    if _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log(severity: str, message: str, **fields):
    _logger.info(json.dumps({"severity": severity, "message": message, **fields}))


def log_request(method: str, route: str, status: int, ms: float, revision: str):
    if _quiet:
        return
    log(
        "ERROR" if status >= 500 else "INFO",
        "request",
        route=f"{method} {route}",
        status=status,
        latency_ms=round(ms, 2),
        revision=revision,
    )
