"""Console + rotating file logging, with a safety net that scrubs key material."""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

LOGGER_NAME = "oci_a1_retry"
LOG_FILENAME = "oci-a1-retry.log"
MAX_LOG_BYTES = 2_000_000
LOG_BACKUPS = 5  # at most ~12 MB of logs in total
PLAIN = {"plain": True}  # extra= marker: print the console line without a timestamp

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(pass_?phrase|security_token)(\s*[=:]\s*)\S+")
_TRACEBACK_FORMATTER = logging.Formatter()


def redact(text: str) -> str:
    text = _PEM_BLOCK.sub("<redacted private key>", text)
    return _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)


class RedactingFilter(logging.Filter):
    """Defense in depth: the app never logs keys, but scrub them if one slips through."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        # Formatters reuse exc_text when it is set, so tracebacks get scrubbed too.
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(_TRACEBACK_FORMATTER.formatException(record.exc_info))
        return True


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, "plain", False):
            return record.getMessage()
        return super().format(record)


def setup_logging(
    *,
    verbose: bool,
    log_dir: Path | None,
    stream: TextIO | None = None,
    console_level: int | None = None,
) -> Path | None:
    """Configure the application logger; returns the log file path (None if unavailable)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # The SDK's own debug output includes request details; keep it quiet.
    logging.getLogger("oci").setLevel(logging.WARNING)

    console = logging.StreamHandler(stream or sys.stdout)
    console.setLevel(console_level or (logging.DEBUG if verbose else logging.INFO))
    # systemd sets JOURNAL_STREAM when output goes to the journal, which adds its own time.
    console_format = (
        "%(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(message)s"
    )
    console.setFormatter(_ConsoleFormatter(console_format, "%H:%M:%S"))
    console.addFilter(RedactingFilter())
    logger.addHandler(console)

    if log_dir is None:
        return None
    log_file = log_dir / LOG_FILENAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Cannot write log file %s (%s); logging to console only.", log_file, exc)
        return None
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)
    return log_file
