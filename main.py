"""Entry point for PyInstaller EXE and direct launch."""
import sys
import os
import traceback
from pathlib import Path

# Ensure logs directory exists FIRST — before any other imports
# that could fail and need a place to write crash logs.
if getattr(sys, 'frozen', False):
    app_root = Path(sys.executable).parent
else:
    app_root = Path(__file__).parent
logs_dir = app_root / "logs"
logs_dir.mkdir(exist_ok=True)


def main():
    """Launch TG Web Auth GUI with crash protection."""
    try:
        from src.gui.app import main as gui_main
        gui_main()
    except Exception as e:
        error_msg = f"Fatal error: {e}"
        # Write to crash log
        try:
            crash_file = logs_dir / "last_crash.txt"
            crash_file.write_text(error_msg + chr(10) + chr(10) + traceback.format_exc())
        except Exception:
            pass
        # Show GUI error if running as frozen EXE (console=False means no stdout)
        if getattr(sys, 'frozen', False):
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("TGWebAuth Error", error_msg)
                root.destroy()
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
