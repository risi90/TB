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


def setup_logging(level: str = "INFO") -> None:
    """Configure the global loguru logger for clean console output.

    Args:
        level: Minimum log level (e.g. ``"DEBUG"``, ``"INFO"``).
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
