"""
Standardized Logging Utilities for Upstox Algorithmic Trading Infrastructure.
Provides consistent timestamped logging across all core and execution services.
Format: [YYYY-MM-DD HH:MM:SS.mmm] [LEVEL] [LOGGER] Message
"""
import logging
import os
import sys
from typing import Optional

DEFAULT_LOG_FORMAT = "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_standard_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    logger_name: Optional[str] = None,
) -> logging.Logger:
    """Configure and return a standardized logger with millisecond timestamps."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if already configured
    if not logger.handlers:
        formatter = logging.Formatter(fmt=DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

        # Console handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # File handler if specified
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(formatter)
            logger.addHandler(fh)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Get or create standard logger for a module."""
    return setup_standard_logging(logger_name=name)
