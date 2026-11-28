"""
Standardized Logging Utilities for Antigravity Trading Infrastructure.
Provides unified IST timestamped structured logging with auto-flush and rotating file support.
Format: [YYYY-MM-DD HH:MM:SS.mmm IST] [LEVEL] [LOGGER] Message
"""
from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from typing import Optional

from core.common.time_utils import IST

# Standardize level names cleanly as INFO / WARN / ERROR / CRITICAL / DEBUG
logging.addLevelName(logging.WARNING, "WARN")

DEFAULT_LOG_FORMAT = "[%(asctime)s IST] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class ISTFormatter(logging.Formatter):
    """Logging formatter emitting millisecond timestamps strictly in IST."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IST)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d}"


class FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes immediately so redirected daemon logs update live."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


class FlushingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes immediately so Health Tab reads fresh logs."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def setup_standard_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    logger_name: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure and return a standardized IST logger with auto-flushing stdout and optional rotating file."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    if logger_name:
        logger.propagate = False

    formatter = ISTFormatter(fmt=DEFAULT_LOG_FORMAT)

    if not logger.handlers:
        ch = FlushingStreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    if log_file:
        abs_log = os.path.abspath(log_file)
        has_file = any(
            isinstance(h, logging.FileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == abs_log
            for h in logger.handlers
        )
        if not has_file:
            os.makedirs(os.path.dirname(abs_log), exist_ok=True)
            fh = FlushingRotatingFileHandler(abs_log, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
            fh.setFormatter(formatter)
            logger.addHandler(fh)

    return logger


def get_logger(
    name: str,
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """Get or create standardized module logger."""
    return setup_standard_logging(level=level, log_file=log_file, logger_name=name)
