"""
Unified logging configuration for tg-web-auth.

Usage:
    from src.logger import get_logger
    logger = get_logger(__name__)

    logger.debug("Detailed info for debugging")
    logger.info("Progress information")
    logger.warning("Something unexpected but handled")
    logger.error("Error that affects functionality")
"""

import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    format_string: Optional[str] = None
) -> None:
    """
    Configure root logger with consistent formatting.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path to write logs
        format_string: Custom format string (uses default if None)
    """
    if format_string is None:
        format_string = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler (stderr for visibility)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(format_string, datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(format_string, datefmt="%Y-%m-%d %H:%M:%S"))
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given module name.

    Args:
        name: Module name (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


# Default setup on import (can be reconfigured by CLI)
_initialized = False

def _ensure_initialized() -> None:
    """Initialize logging with defaults if not already done."""
    global _initialized
    if not _initialized:
        setup_logging(level=logging.INFO)
        _initialized = True


# Auto-initialize with defaults
_ensure_initialized()
