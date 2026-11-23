"""JSON logs, one object per line, safe to ship to Cloud Logging.

Rules: never log an API key, a full target URL, a query string or request
headers. Log the target as scheme://host only, and keys by their numeric id.

One exception to "no headers", and it is narrow: a request line names the
software that sent it (``client_software``): the first word of its
``User-Agent`` and a coarse kind, never the header itself. An MCP
``initialize`` adds the name and version the client reports (``clean_name``).
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


# --- which software is asking -----------------------------------------------------------

AGENT_MAX = 32
MCP_CLIENT_MAX = 40
MCP_CLIENT_VERSION_MAX = 20

# A User-Agent with one of these anywhere in it (any case) is a ``bot``: crawlers, link
# previewers, uptime checks, scanners, headless browsers. ``compatible;`` is how a program
# that is not a browser marks a ``Mozilla/`` header (Googlebot, Claude-User, ChatGPT-User).
# ``fetcher`` and not ``fetch``: node-fetch is an HTTP library that real clients are built on.
BOT_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "preview",
    "monitor",
    "uptime",
    "scan",
    "fetcher",
    "headless",
    "lighthouse",
    "externalhit",
    "compatible;",
)

# The most of a value that is looked at before it is cut to its limit.
_NAME_SCAN = 256
_TOKEN_END_RE = re.compile(r"[/\s]")
_NOT_NAME_RE = re.compile(r"[^a-z0-9._-]")
_SPACED_RE = re.compile(r"\s*-\s*|\s+")


def clean_name(value: object, limit: int) -> str | None:
    """A name a client gave for itself, made safe to log: lower case, ``[a-z0-9._-]`` only, cut to ``limit``.

    Spaces become ``-`` and every other character is dropped. ``None`` for anything
    that is not a string, has no letter or digit left, or looks like an API key.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()[:_NAME_SCAN]
    if _KEY_RE.search(text):
        return None
    text = _NOT_NAME_RE.sub("", _SPACED_RE.sub("-", text.lower()))[:limit]
    return text if any(c.isalnum() for c in text) else None


def client_software(user_agent: str | None) -> tuple[str | None, str]:
    """``(agent, agent_kind)`` for a request log line, from its ``User-Agent`` header.

    ``agent`` is the header's first product token, the text before the first ``/``
    or space (``mozilla``, ``curl``, ``python-httpx``), cleaned by ``clean_name``.
    ``agent_kind`` is ``none`` (no header), ``bot`` (a ``BOT_MARKERS`` word anywhere
    in it), ``browser`` (starts with ``Mozilla/``) or ``tool`` (anything else).
    The header itself is never logged or kept.
    """
    header = (user_agent or "").strip()
    if not header:
        return None, "none"
    agent = clean_name(_TOKEN_END_RE.split(header, maxsplit=1)[0], AGENT_MAX)
    lowered = header.lower()
    if any(marker in lowered for marker in BOT_MARKERS):
        return agent, "bot"
    return agent, "browser" if lowered.startswith("mozilla/") else "tool"


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
