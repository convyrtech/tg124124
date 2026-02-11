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
