"""Centralized path resolution for dev and frozen (PyInstaller) modes."""
import sys
from pathlib import Path


def get_app_root() -> Path:
    """Get application root directory.

    In frozen (PyInstaller) mode: directory containing the EXE.
    In dev mode: project root (parent of src/).
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


APP_ROOT = get_app_root()
ACCOUNTS_DIR = APP_ROOT / "accounts"
PROFILES_DIR = APP_ROOT / "profiles"
DATA_DIR = APP_ROOT / "data"
LOGS_DIR = APP_ROOT / "logs"


def _check_ascii_path():
    """Warn if APP_ROOT contains non-ASCII characters that may cause issues."""
    try:
        str(APP_ROOT).encode('ascii')
    except UnicodeEncodeError:
        import warnings
        warnings.warn(
            f"APP_ROOT contains non-ASCII characters: {APP_ROOT}. "
            "Some components (SQLite, pproxy) may have issues. "
            "Consider moving the application to a path with only ASCII characters.",
            RuntimeWarning,
            stacklevel=2
        )


_check_ascii_path()
