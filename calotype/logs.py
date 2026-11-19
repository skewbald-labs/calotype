"""JSON logs, one object per line, safe to ship to Cloud Logging.

Rules: never log an API key, a full target URL, a query string or request
headers. Log the target as scheme://host only, and keys by their numeric id.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# Any URL keeps only its scheme and host; keys and access_key values are masked.
_URL_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s\"'?#]*)[^\s\"']*")
_KEY_RE = re.compile(r"\bcal_[A-Za-z0-9_-]{8,}")
_ACCESS_KEY_RE = re.compile(r"(access_key=)[^&\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(bearer\s+)[^\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    text = _ACCESS_KEY_RE.sub(r"\1[redacted]", text)
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    text = _KEY_RE.sub("cal_[redacted]", text)
    return _URL_RE.sub(r"\1", text)

_LEVEL_TO_SEVERITY = {"WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "INFO": "INFO", "DEBUG": "DEBUG"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": _LEVEL_TO_SEVERITY.get(record.levelname, record.levelname),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        if record.exc_info:
            out["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's access log prints the raw path with its query string, which can
    # hold ?access_key=. It is switched off; the app logs requests itself.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").propagate = False
    # HTTP client libraries log full request URLs at INFO.
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The MCP SDK logs every stateless request at INFO; the app logs requests itself.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, message, extra={"fields": fields})
