"""Structured console logging built on loguru."""

from __future__ import annotations

import sys

from loguru import logger

_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan> | "
    "<level>{message}</level>"
)


_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name} | {message}"
)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure the global loguru logger for console and optional file output.

    Args:
        level: Minimum log level (e.g. ``"DEBUG"``, ``"INFO"``).
        log_file: Optional path for a rotating plain-text log file; the
            dashboard's *Live Logs* tab tails this file.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=_FORMAT,
        colorize=True,
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )
    if log_file:
        logger.add(
            log_file,
            level=level.upper(),
            format=_FILE_FORMAT,
            rotation="10 MB",
            retention=5,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )
