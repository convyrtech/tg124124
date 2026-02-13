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
        # Truncate if grown too large (>1MB) to prevent unbounded growth
        try:
            if crash_path.exists() and crash_path.stat().st_size > 1_000_000:
                crash_path.unlink()
        except OSError:
            pass
        with open(crash_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Crash at: {datetime.now().isoformat()}\n")
            f.write(f"Exception: {exc_type.__name__}: {exc_value}\n\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception as write_err:
        # Last resort: stderr (None in frozen windowed mode)
        if sys.stderr is not None:
            try:
                print(f"[CRASH HANDLER] Could not write crash file: {write_err}", file=sys.stderr)
                traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
            except Exception:
                pass  # Truly nothing we can do


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
        logger.error(
            "Asyncio unhandled: %s — %s", message, exception,
            exc_info=(type(exception), exception, exception.__traceback__)
        )
        _write_crash_file(type(exception), exception, exception.__traceback__)
    else:
        logger.error("Asyncio error: %s", message)


def install_exception_handlers() -> None:
    """Install global exception handlers. Call once at app startup."""
    sys.excepthook = _excepthook
    logger.debug("Global exception handler installed")


def install_asyncio_handler(loop) -> None:
    """Install exception handler on asyncio event loop.

    Call after event loop creation (e.g. in GUI background thread).

    Args:
        loop: asyncio event loop instance
    """
    loop.set_exception_handler(_asyncio_exception_handler)
    logger.debug("Asyncio exception handler installed on loop")
