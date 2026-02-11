"""Global exception handler for crash diagnostics.

Hooks into sys.excepthook and asyncio exception handler to:
- Log unhandled exceptions
- Save last crash info to logs/last_crash.txt
"""

import logging
import sys
import traceback
from datetime import datetime

from .paths import LOGS_DIR

logger = logging.getLogger(__name__)


def _write_crash_file(exc_type, exc_value, exc_tb) -> None:
    """Write crash details to logs/last_crash.txt."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        crash_path = LOGS_DIR / "last_crash.txt"
        with open(crash_path, 'w', encoding='utf-8') as f:
            f.write(f"Crash at: {datetime.now().isoformat()}\n")
            f.write(f"Exception: {exc_type.__name__}: {exc_value}\n\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception:
        pass  # Don't crash in crash handler


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """Global exception handler for unhandled exceptions."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    logger.critical(
        "Unhandled exception: %s: %s", exc_type.__name__, exc_value,
        exc_info=(exc_type, exc_value, exc_tb)
    )
    _write_crash_file(exc_type, exc_value, exc_tb)


def _asyncio_exception_handler(loop, context) -> None:
    """Handle uncaught asyncio exceptions."""
    exception = context.get('exception')
    message = context.get('message', 'Unknown asyncio error')

    if exception:
        logger.error("Asyncio unhandled: %s — %s", message, exception, exc_info=exception)
        _write_crash_file(type(exception), exception, exception.__traceback__)
    else:
        logger.error("Asyncio error: %s", message)


def install_exception_handlers() -> None:
    """Install global exception handlers. Call once at app startup."""
    sys.excepthook = _excepthook
    logger.debug("Global exception handler installed")
