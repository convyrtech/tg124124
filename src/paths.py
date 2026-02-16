"""Centralized path resolution for dev and frozen (PyInstaller) modes."""

import sys
from pathlib import Path


def get_app_root() -> Path:
    """Get application root directory.

    In frozen (PyInstaller) mode: directory containing the EXE.
    In dev mode: project root (parent of src/).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


APP_ROOT = get_app_root()
ACCOUNTS_DIR = APP_ROOT / "accounts"
PROFILES_DIR = APP_ROOT / "profiles"
DATA_DIR = APP_ROOT / "data"
LOGS_DIR = APP_ROOT / "logs"


def to_relative_path(abs_path: Path) -> str:
    """Convert absolute path to relative from APP_ROOT for portable DB storage."""
    try:
        return str(abs_path.relative_to(APP_ROOT))
    except ValueError:
        return str(abs_path)  # Already relative or different root


def resolve_path(db_path: str) -> Path:
    """Resolve a DB-stored path (possibly relative) to absolute.

    Handles backward compatibility: old DBs store absolute paths,
    new DBs store relative paths from APP_ROOT.
    """
    p = Path(db_path)
    if p.is_absolute():
        return p  # Backward compat with old DBs
    return APP_ROOT / p


def _check_ascii_path():
    """Warn if APP_ROOT contains non-ASCII characters that may cause issues."""
    try:
        str(APP_ROOT).encode("ascii")
    except UnicodeEncodeError:
        import warnings

        warnings.warn(
            f"APP_ROOT contains non-ASCII characters: {APP_ROOT}. "
            "Some components (SQLite, pproxy) may have issues. "
            "Consider moving the application to a path with only ASCII characters.",
            RuntimeWarning,
            stacklevel=2,
        )


_check_ascii_path()
