"""
logger.py — Structured logging of every decision, signal, and order.

Uses loguru for both console and rotating file output.
Log files are kept in logs/ directory, max 10 MB each, retained for 7 days.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger as _base_logger


def setup_logger(log_level: str = "DEBUG") -> None:
    """Configure loguru sinks: console (colored) + rotating file."""
    # Remove default sink
    _base_logger.remove()

    # Console sink — colorized, human-readable
    _base_logger.add(
        sink=sys.stderr,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # Rotating file sink — structured, no colors
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _base_logger.add(
        sink=str(log_dir / "trading_bot_{time:YYYY-MM-DD}.log"),
        level=log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        enqueue=True,  # thread-safe
    )


def get_logger():
    """Return the configured loguru logger instance."""
    return _base_logger
