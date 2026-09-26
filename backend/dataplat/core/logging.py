"""Structured logging with secret redaction.

A redaction filter runs on every record before it is formatted, masking anything
that looks like a credential, so a stray ``log.info(conn_string)`` can't leak one.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # user:password@host in URLs / DSNs
    (re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(?=@)", re.I), r"\g<pre>***"),
    # key=value / key: value for secret-ish keys (vault:// references are not secrets)
    (
        re.compile(
            r"(?P<pre>\b(?:password|passwd|pwd|secret|secret_id|token|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|authorization)\b\"?\s*[=:]\s*\"?)(?!vault://)(?:Bearer\s+)?[^\s\"',;&]+",
            re.I,
        ),
        r"\g<pre>***",
    ),
    # Vault tokens and bearer JWTs
    (re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}"), "***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "***"),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "***"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            entry["exc"] = record.exc_text
        return json.dumps(entry)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library loggers (uvicorn, hvac, kafka) propagate to root and get the same filter.
    # APScheduler logs every interval run at INFO; keep only its warnings.
    logging.getLogger("apscheduler").setLevel("WARNING")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
