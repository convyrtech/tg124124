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
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .paths import LOGS_DIR


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    format_string: Optional[str] = None,
    enable_file_logging: bool = True
) -> None:
    """
    Configure root logger with consistent formatting.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path to write logs (overrides default)
        format_string: Custom format string (uses default if None)
        enable_file_logging: If True, always write to LOGS_DIR/app.log with rotation

    Environment:
        TGWA_DEBUG: Set to '1' or 'true' to force DEBUG level logging
    """
    # Override level if TGWA_DEBUG env var is set
    if os.environ.get('TGWA_DEBUG', '').lower() in ('1', 'true'):
        level = logging.DEBUG

    if format_string is None:
        format_string = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

    formatter = logging.Formatter(format_string, datefmt="%Y-%m-%d %H:%M:%S")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler (stderr for visibility)
    # In frozen windowed mode (console=False), sys.stderr is None
    if sys.stderr is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # Rotating file handler — always enabled (5MB x 3 files)
    if enable_file_logging:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            rotating_handler = RotatingFileHandler(
                LOGS_DIR / "app.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding='utf-8'
            )
            rotating_handler.setLevel(level)
            rotating_handler.setFormatter(formatter)
            root_logger.addHandler(rotating_handler)
        except Exception as e:
            # Don't crash if log dir is read-only
            if sys.stderr is not None:
                print(f"Warning: could not enable file logging: {e}", file=sys.stderr)

    # Optional additional file handler (for CLI --log-file)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
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
