"""Main GUI application."""

import asyncio
import collections
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

import dearpygui.dearpygui as dpg

from .. import __version__
from ..database import AccountRecord
from ..utils import sanitize_error as _se
from .controllers import AppController
from .theme import create_hacker_theme, create_status_themes

logger = logging.getLogger(__name__)


def _select_folder_tkinter() -> Path | None:
    """Use tkinter for folder selection (more stable than dpg file dialog)."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        # Create hidden root window
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        folder = filedialog.askdirectory(title="Select Sessions Folder")
        root.destroy()

        if folder:
            return Path(folder)
        return None
    except Exception as e:
        logger.exception("Tkinter folder dialog error: %s", e)
        return None


def _select_file_tkinter(title: str = "Select File", filetypes: list = None) -> Path | None:
    """Use tkinter for file selection."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        if filetypes is None:
            filetypes = [("Text files", "*.txt"), ("All files", "*.*")]

        filepath = filedialog.askopenfilename(title=title, filetypes=filetypes)
        root.destroy()

        if filepath:
            return Path(filepath)
        return None
    except Exception as e:
        logger.exception("Tkinter file dialog error: %s", e)
        return None


class TGWebAuthApp:
    """Main application window."""

    def __init__(self, data_dir: Path | None = None):
        from ..paths import DATA_DIR

        self.data_dir = data_dir or DATA_DIR
        self._2fa_password: str | None = None
        self._status_themes: dict = {}
        self._controller = AppController(self.data_dir)
        self._async_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_ready = threading.Event()  # Signals async loop is ready
        self._async_init_error: str | None = None  # Stores init failure reason
        # Thread-safe queue for UI updates from async code
        self._ui_queue: queue.Queue = queue.Queue()
        # Track active browser contexts for cleanup
        self._active_browsers: list = []
        self._shutting_down = threading.Event()  # Thread-safe shutdown flag

        self._active_pool = None
        # Guard against double-click on non-batch buttons (open, import, proxy ops)
        self._busy_ops: set = set()
        self._last_table_refresh: float = 0.0
        # Fix #10: O(1) log append with bounded deque instead of O(n) string concat
        # FIX-7.4: 500→5000 for 1000-account batches (~3-4 messages per account)
        self._log_lines: collections.deque = collections.deque(maxlen=5000)
        self._log_lines.append("[System] TG Web Auth started")
        # Fix #9: Track per-row action buttons for disabling during batch
        self._row_action_buttons: list[int] = []
        # Fix #11: Track status/fragment cells for incremental updates
        self._status_cells: dict[int, int] = {}
        self._fragment_cells: dict[int, int] = {}

    def _start_async_loop(self) -> None:
        """Start async event loop in background thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            # Install asyncio crash handler on this loop
            from ..exception_handler import install_asyncio_handler

            install_asyncio_handler(self._loop)

            # Initialize controller
            self._loop.run_until_complete(self._controller.initialize())
            self._async_ready.set()

            # Keep loop running
            self._loop.run_forever()
        except Exception as e:
            logger.critical("Async loop init failed: %s", e, exc_info=True)
            self._async_init_error = str(e)
            self._async_ready.set()  # Unblock main thread even on failure

    def _run_async(self, coro) -> None:
        """Schedule coroutine on async loop."""
        if self._shutting_down.is_set() or not self._loop or self._loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._on_async_done)

    def _on_async_done(self, future) -> None:
        """Handle completion of async tasks - log errors only.

        IMPORTANT: Do NOT reset _active_pool here! This callback fires for
        ALL async tasks (search, import, delete, etc.), not just batch ops.
        Resetting pool state on an unrelated failure (e.g. search error)
        would re-enable batch buttons while a batch is still running,
        allowing a second concurrent batch → AUTH_KEY_DUPLICATED.
        Batch methods manage their own pool cleanup in finally blocks.
        """
        try:
            future.result()
        except Exception as e:
            logger.exception("Async task failed: %s", e)
            self._log(f"[Error] Background task failed: {_se(str(e))}")

    def _schedule_ui(self, func: Callable) -> None:
        """Schedule a function to run on the main UI thread."""
        if self._shutting_down.is_set():
            return
        self._ui_queue.put(func)

    def _process_ui_queue(self) -> None:
        """Process pending UI updates (call from main thread)."""
        while not self._ui_queue.empty():
            try:
                func = self._ui_queue.get_nowait()
                func()
            except Exception as e:
                logger.exception("UI queue error: %s", e)
                self._log(f"[Error] UI update failed: {_se(str(e))}")

    def _shutdown(self) -> None:
        """Clean shutdown - close all browsers and stop async loop."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        logger.info("Shutting down application...")

        # Stop active worker pool — request shutdown and give workers time to finish
        # their current tasks gracefully (prevents mid-flight browser/Telethon interruption
        # which could corrupt session files).
        pool = getattr(self, "_active_pool", None)
        if pool and pool != "pending":
            pool.request_shutdown()
            # Wait for pool workers to finish current tasks.
            # pool.run() is running as a coroutine on the async loop — it checks
            # shutdown_event and drains gracefully. We poll _active_pool which is
            # set to None by _batch_migrate/_batch_fragment after pool.run() returns.
            if self._loop:
                import time as _time

                deadline = _time.monotonic() + 30
                while self._active_pool is not None and _time.monotonic() < deadline:
                    _time.sleep(0.5)
                if self._active_pool is not None:
                    logger.warning("Pool did not finish within 30s, proceeding with shutdown")
            self._active_pool = None

        # Close all tracked browser contexts
        if self._loop and self._active_browsers:

            async def close_browsers():
                for ctx in list(self._active_browsers):  # Defensive copy for thread safety
                    try:
                        await ctx.close()
                        logger.info("Closed browser context")
                    except Exception as e:
                        logger.warning("Error closing browser: %s", e)
                self._active_browsers.clear()

            try:
                future = asyncio.run_coroutine_threadsafe(close_browsers(), self._loop)
                future.result(timeout=10)  # Wait up to 10 seconds
            except Exception as e:
                logger.warning("Error during browser cleanup: %s", e)

        # Shutdown controller (close database)
        if self._loop and self._controller:
            try:
                future = asyncio.run_coroutine_threadsafe(self._controller.shutdown(), self._loop)
                future.result(timeout=5)
            except Exception as e:
                logger.warning("Error during controller shutdown: %s", e)

        # Stop the async loop
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                # P2 FIX: Loop may already be closed/stopped
                pass
            # Wait for thread to finish
            if self._async_thread and self._async_thread.is_alive():
                self._async_thread.join(timeout=5)

        # Last resort: kill any orphaned child processes (pproxy, camoufox)
        try:
            import psutil

            current = psutil.Process()
            children = current.children(recursive=True)
            for child in children:
                try:
                    child_name = child.name()  # Cache before kill (process may be reaped)
                    child.kill()
                    logger.info("Killed orphan child process: PID=%d (%s)", child.pid, child_name)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception as e:
            logger.warning("Child process cleanup error: %s", e)

        logger.info("Shutdown complete")

    def run(self) -> None:
        """Start the application."""
        # Start async thread first
        self._async_thread = threading.Thread(target=self._start_async_loop, daemon=True)
        self._async_thread.start()

        # Wait for async loop to initialize (up to 10s)
        self._async_ready.wait(timeout=10)
        if self._async_init_error:
            from ..utils import sanitize_error
            err = sanitize_error(self._async_init_error)
            logger.critical("Cannot start: async init failed: %s", err)
            print(f"FATAL: Failed to initialize: {err}", file=sys.stderr)
            # Continue to show GUI with error message displayed to user

        dpg.create_context()

        # Load font with Cyrillic support
        # Auto-download JetBrainsMono if not present
        try:
            font_loaded = False
            import os
            import urllib.request

            appdata = os.environ.get("APPDATA", "")
            font_dir = Path(appdata) / "tgwebauth"
            font_path = font_dir / "JetBrainsMono-Regular.ttf"

            if not font_path.exists():
                logger.info("Font not found, downloading JetBrainsMono...")
                font_dir.mkdir(parents=True, exist_ok=True)
                font_url = "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-Regular.ttf"
                try:
                    import socket

                    _old_timeout = socket.getdefaulttimeout()
                    socket.setdefaulttimeout(5)
                    try:
                        urllib.request.urlretrieve(font_url, font_path)
                    finally:
                        socket.setdefaulttimeout(_old_timeout)
                    logger.info("Font downloaded to: %s", font_path)
                except Exception as e:
                    logger.warning("Font download failed: %s", e)

            if font_path.exists():
                font_str = str(font_path)
                with dpg.font_registry():
                    with dpg.font(font_str, 16) as default_font:
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Cyrillic)
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                dpg.bind_font(default_font)
                font_loaded = True
                logger.info("Cyrillic font loaded successfully")

            if not font_loaded:
                logger.warning("Font not available - Cyrillic may not display correctly")
        except Exception as e:
            logger.warning("Font loading failed: %s - using default font", e)

        # Apply theme
        theme = create_hacker_theme()
        dpg.bind_theme(theme)
        self._status_themes = create_status_themes()

        # Create main window
        self._create_main_window()

        # Create viewport
        dpg.create_viewport(title=f"TG Web Auth v{__version__}", width=1200, height=800, min_width=800, min_height=600)

        dpg.setup_dearpygui()
        dpg.show_viewport()

        # Show 2FA dialog on start
        self._show_2fa_dialog()

        # Register atexit + signal handlers for safety
        import atexit
        import signal

        atexit.register(self._shutdown)
        def _sigint_handler(*_):
            if self._shutting_down.is_set():
                # Second Ctrl+C: force-kill immediately
                logger.warning("Force-kill on second SIGINT")
                os._exit(1)
            self._shutdown()

        signal.signal(signal.SIGINT, _sigint_handler)
        # SIGTERM is not delivered on Windows, but register for POSIX compatibility
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, lambda *_: self._shutdown())

        # Custom render loop to process UI queue
        while dpg.is_dearpygui_running():
            self._process_ui_queue()
            self._maybe_periodic_refresh()
            dpg.render_dearpygui_frame()

        # Clean shutdown before destroying context
        self._shutdown()
        dpg.destroy_context()

    def _create_main_window(self) -> None:
        """Create main application window."""
        with dpg.window(tag="main_window", label="TG Web Auth"):
            # Header with stats
            with dpg.group(horizontal=True):
                dpg.add_text("Accounts:", color=(150, 150, 150))
                dpg.add_text("0", tag="stat_total")
                dpg.add_spacer(width=20)

                dpg.add_text("[OK]", color=(46, 204, 64))
                dpg.add_text("0", tag="stat_healthy")
                dpg.add_spacer(width=10)

                dpg.add_text("[~]", color=(66, 165, 245))
                dpg.add_text("0", tag="stat_migrating")
                dpg.add_spacer(width=10)

                dpg.add_text("[X]", color=(255, 82, 82))
                dpg.add_text("0", tag="stat_errors")
                dpg.add_spacer(width=10)

                dpg.add_text("[F]", color=(255, 215, 0))
                dpg.add_text("0", tag="stat_fragment")

                dpg.add_spacer(width=50)
                dpg.add_text("Proxies:", color=(150, 150, 150))
                dpg.add_text("0/0", tag="stat_proxies")

            dpg.add_separator()

            # Tab bar
            with dpg.tab_bar():
                with dpg.tab(label="Accounts"):
                    self._create_accounts_tab()

                with dpg.tab(label="Proxies"):
                    self._create_proxies_tab()

                with dpg.tab(label="Logs"):
                    self._create_logs_tab()

        dpg.set_primary_window("main_window", True)

    def _create_accounts_tab(self) -> None:
        """Create accounts management tab."""
        # Toolbar
        with dpg.group(horizontal=True):
            dpg.add_input_text(
                tag="account_search", hint="Search accounts...", width=300, callback=self._on_search_accounts
            )
            dpg.add_spacer(width=20)
            dpg.add_button(label="Import Sessions", callback=self._show_import_dialog)
            dpg.add_button(label="Migrate Selected", tag="btn_migrate_selected", callback=self._migrate_selected)
            dpg.add_button(label="Migrate All", tag="btn_migrate_all", callback=self._migrate_all)
            dpg.add_button(label="Retry Failed", tag="btn_retry_failed", callback=self._retry_failed)
            dpg.add_button(label="Fragment All", tag="btn_fragment_all", callback=self._fragment_all)
            dpg.add_button(label="STOP", tag="btn_stop", callback=self._stop_migration)
            dpg.add_spacer(width=20)
            dpg.add_button(label="Auto-Assign Proxies", callback=self._auto_assign_proxies)

        dpg.add_spacer(height=10)

        # Accounts table
        with dpg.table(
            tag="accounts_table",
            header_row=True,
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            borders_outerV=True,
            row_background=True,
            resizable=True,
            sortable=True,
        ):
            dpg.add_table_column(label="", width_fixed=True, width=30)  # Checkbox
            dpg.add_table_column(label="Name", width=200)
            dpg.add_table_column(label="Username", width=150)
            dpg.add_table_column(label="Status", width=80)
            dpg.add_table_column(label="Fragment", width=80)
            dpg.add_table_column(label="Proxy", width=160)
            dpg.add_table_column(label="Actions", width=150)

    def _create_proxies_tab(self) -> None:
        """Create proxies management tab."""
        with dpg.group(horizontal=True):
            dpg.add_button(label="Import Proxies", callback=self._show_proxy_import_dialog)
            dpg.add_button(label="Check All", callback=self._check_all_proxies)
            dpg.add_button(label="Replace Dead", callback=self._replace_dead_proxies)

        dpg.add_spacer(height=10)

        with dpg.table(
            tag="proxies_table",
            header_row=True,
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            borders_outerV=True,
            row_background=True,
            resizable=True,
        ):
            dpg.add_table_column(label="Host:Port", width=180)
            dpg.add_table_column(label="Protocol", width=80)
            dpg.add_table_column(label="Status", width=80)
            dpg.add_table_column(label="Assigned To", width=150)
            dpg.add_table_column(label="Actions", width=100)

    def _create_logs_tab(self) -> None:
        """Create logs tab."""
        with dpg.group(horizontal=True):
            dpg.add_button(label="Collect Logs", callback=self._collect_diagnostics)
            dpg.add_text("", tag="diagnostics_status", color=(150, 150, 150))

        dpg.add_spacer(height=5)
        dpg.add_input_text(
            tag="log_output",
            multiline=True,
            readonly=True,
            width=-1,
            height=-1,
            default_value="[System] TG Web Auth started\n",
        )

    def _collect_diagnostics(self, sender=None, app_data=None) -> None:
        """Collect logs + system info into a ZIP for support (runs async to avoid GUI freeze)."""
        if "collect_diagnostics" in self._busy_ops:
            self._log("[Diagnostics] Already collecting...")
            return
        self._busy_ops.add("collect_diagnostics")
        self._schedule_ui(lambda: dpg.set_value("diagnostics_status", "Collecting..."))
        self._run_async(self._collect_diagnostics_async())

    async def _collect_diagnostics_async(self) -> None:
        """Async wrapper: offloads heavy I/O (rglob, DB copy) to executor."""
        try:
            import os
            import platform
            import subprocess as sp
            import zipfile
            from datetime import datetime

            from ..paths import APP_ROOT, DATA_DIR, LOGS_DIR

            def _build_zip() -> str:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                zip_name = f"diagnostics_{timestamp}.zip"
                zip_path = APP_ROOT / zip_name

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    # app.log (last 5000 lines) + rotated logs
                    from ..utils import sanitize_error

                    log_file = LOGS_DIR / "app.log"
                    if log_file.exists():
                        try:
                            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                            sanitized_lines = [sanitize_error(line) for line in lines[-5000:]]
                            zf.writestr("app.log", "\n".join(sanitized_lines))
                        except Exception as e:
                            logger.warning("Diagnostics: failed to collect app.log: %s", e)
                            zf.writestr("app.log.error", f"Could not include app.log: {_se(str(e))}")

                    # Include rotated log files (app.log.1 .. app.log.5)
                    for i in range(1, 6):
                        rotated = LOGS_DIR / f"app.log.{i}"
                        if rotated.exists():
                            try:
                                rot_lines = rotated.read_text(encoding="utf-8", errors="replace").splitlines()
                                sanitized_rot = [sanitize_error(line) for line in rot_lines[-5000:]]
                                zf.writestr(f"app.log.{i}", "\n".join(sanitized_rot))
                            except Exception as e:
                                logger.warning("Diagnostics: failed to collect app.log.%d: %s", i, e)

                    # last_crash.txt (sanitized)
                    crash_file = LOGS_DIR / "last_crash.txt"
                    if crash_file.exists():
                        try:
                            crash_text = crash_file.read_text(encoding="utf-8", errors="replace")
                            zf.writestr("last_crash.txt", sanitize_error(crash_text))
                        except Exception as e:
                            logger.warning("Diagnostics: failed to collect last_crash.txt: %s", e)

                    # Database copy (strip proxy credentials)
                    db_file = DATA_DIR / "tgwebauth.db"
                    if db_file.exists():
                        tmp_path = None
                        try:
                            import sqlite3
                            import tempfile

                            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                                tmp_path = tmp.name
                            # Use SQLite backup API instead of file copy:
                            # - handles WAL mode correctly (no WinError 32)
                            # - produces a consistent snapshot even with active connections
                            src_conn = sqlite3.connect(db_file)
                            dst_conn = sqlite3.connect(tmp_path)
                            try:
                                try:
                                    src_conn.backup(dst_conn)
                                finally:
                                    src_conn.close()
                                # Sanitize proxy credentials (table may not exist if DB is fresh)
                                try:
                                    dst_conn.execute("UPDATE proxies SET username=NULL, password=NULL")
                                    dst_conn.commit()
                                except sqlite3.OperationalError:
                                    pass  # Table doesn't exist yet — OK
                            finally:
                                dst_conn.close()
                            zf.write(tmp_path, "tgwebauth_sanitized.db")
                        except Exception as e:
                            logger.warning("DB copy for diagnostics failed: %s", e)
                        finally:
                            if tmp_path and os.path.exists(tmp_path):
                                os.unlink(tmp_path)

                    # System info
                    try:
                        import psutil

                        mem = psutil.virtual_memory()
                        disk = psutil.disk_usage(str(APP_ROOT))
                        system_info = (
                            f"App: TG Web Auth v{__version__}\n"
                            f"OS: {platform.platform()}\n"
                            f"Python: {sys.version}\n"
                            f"Frozen: {getattr(sys, 'frozen', False)}\n"
                            f"CPU cores: {psutil.cpu_count()}\n"
                            f"RAM: {mem.total // (1024**2)} MB (available: {mem.available // (1024**2)} MB)\n"
                            f"Disk: {disk.total // (1024**3)} GB (free: {disk.free // (1024**3)} GB)\n"
                            f"App root: {APP_ROOT}\n"
                        )
                    except Exception:
                        system_info = (
                            f"App: TG Web Auth v{__version__}\n"
                            f"OS: {platform.platform()}\n"
                            f"Python: {sys.version}\n"
                            f"Frozen: {getattr(sys, 'frozen', False)}\n"
                        )
                    try:
                        from camoufox import __version__ as cfx_ver

                        system_info += f"Camoufox: {cfx_ver}\n"
                    except Exception:
                        system_info += "Camoufox: unknown\n"
                    zf.writestr("system_info.txt", system_info)

                    # Profiles summary (count + sizes, no sensitive data)
                    try:
                        from ..paths import PROFILES_DIR

                        prof_lines = []
                        if PROFILES_DIR.exists():
                            for p in sorted(PROFILES_DIR.iterdir()):
                                if p.is_dir():
                                    size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                                    prof_lines.append(f"  {p.name}: {size // 1024} KB")
                        header = f"Total profiles: {len(prof_lines)}\n"
                        zf.writestr("profiles_info.txt", header + "\n".join(prof_lines))
                    except Exception as e:
                        logger.warning("Diagnostics: failed to collect profiles info: %s", e)

                    # Recent errors from operation_log
                    if db_file.exists():
                        try:
                            import sqlite3 as _sql

                            _conn = _sql.connect(str(db_file), timeout=5)
                            try:
                                rows = _conn.execute(
                                    "SELECT created_at, operation, error_message "
                                    "FROM operation_log WHERE success=0 "
                                    "ORDER BY created_at DESC LIMIT 50"
                                ).fetchall()
                            finally:
                                _conn.close()
                            if rows:
                                from ..utils import sanitize_error

                                errors_text = "\n".join(f"{r[0]} | {r[1]} | {sanitize_error(str(r[2]))}" for r in rows)
                                zf.writestr("recent_errors.txt", errors_text)
                        except Exception as e:
                            logger.warning("Diagnostics: failed to collect operation_log: %s", e)

                return zip_name, str(zip_path)

            loop = asyncio.get_running_loop()
            zip_name, zip_path_str = await loop.run_in_executor(None, _build_zip)

            self._log(f"[Diagnostics] Created: {zip_path_str}")
            self._schedule_ui(lambda: dpg.set_value("diagnostics_status", f"Saved: {zip_name}"))

            # Open Explorer with file selected (Windows)
            if sys.platform == "win32":
                try:
                    sp.Popen(["explorer", "/select,", zip_path_str])
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Collect diagnostics error: %s", e)
            self._log(f"[Error] Diagnostics: {_se(str(e))}")
            self._schedule_ui(lambda: dpg.set_value("diagnostics_status", "Error!"))
        finally:
            self._busy_ops.discard("collect_diagnostics")

    def _show_2fa_dialog(self) -> None:
        """Show 2FA password input dialog on startup."""
        with dpg.window(
            tag="2fa_dialog", label="2FA", modal=True, no_close=True, width=450, height=200, pos=[375, 250]
        ):
            dpg.add_text("Пароль двухфакторной аутентификации (2FA)")
            dpg.add_text(
                "Если у ваших аккаунтов есть облачный пароль Telegram,\n"
                "введите его здесь. Если нет — нажмите Пропустить.",
                color=(150, 150, 150),
            )
            dpg.add_spacer(height=10)
            dpg.add_input_text(
                tag="2fa_input",
                password=True,
                hint="Облачный пароль Telegram...",
                width=-1,
                on_enter=True,
                callback=self._on_2fa_submit,
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="OK", width=100, callback=self._on_2fa_submit)
                dpg.add_button(label="Пропустить", width=120, callback=self._on_2fa_skip)

    def _on_2fa_submit(self, sender=None, app_data=None) -> None:
        """Handle 2FA password submission."""
        try:
            password = dpg.get_value("2fa_input")
            if password:
                self._2fa_password = password
                self._log("[System] 2FA password set for session")
            dpg.delete_item("2fa_dialog")
            self._initial_load()
        except Exception as e:
            logger.exception("2FA submit error: %s", e)
            self._log(f"[Error] 2FA: {_se(str(e))}")

    def _on_2fa_skip(self, sender=None, app_data=None) -> None:
        """Skip 2FA password."""
        try:
            dpg.delete_item("2fa_dialog")
            self._log("[System] 2FA password skipped - will prompt when needed")
            self._initial_load()
        except Exception as e:
            logger.exception("2FA skip error: %s", e)
            self._log(f"[Error] 2FA skip: {_se(str(e))}")

    def _initial_load(self) -> None:
        """Load accounts, proxies, and stats on startup."""
        if self._async_init_error:
            from ..utils import sanitize_error
            self._log(f"[FATAL] Initialization failed: {sanitize_error(self._async_init_error)}")
            self._log("[FATAL] Check disk space, permissions, and antivirus settings")
            return

        async def do_load():
            try:
                # Reset interrupted migrations from previous crash
                reset_count = await self._controller.db.reset_interrupted_migrations()
                if reset_count:
                    self._log(f"[System] Reset {reset_count} interrupted migrations")

                accounts = await self._controller.search_accounts("")
                stats = await self._controller.get_stats()
                proxies = await self._controller.db.list_proxies()
                proxies_map = {p.id: p for p in proxies}

                accounts_map = {a.id: a for a in accounts}
                self._schedule_ui(lambda: self._update_stats_sync(stats))
                self._schedule_ui(lambda: self._update_accounts_table_sync(accounts, proxies_map))
                self._schedule_ui(lambda: self._update_proxies_table_sync(proxies, accounts_map))
            except Exception as e:
                logger.exception("Initial load error: %s", e)
                self._log(f"[Error] Initial load: {_se(str(e))}")

        self._run_async(do_load())

    def _log(self, message: str) -> None:
        """Add message to log (thread-safe).

        Uses a bounded deque (maxlen=5000) to avoid O(n^2) string concat.
        FIX-7.1: Rebuild only every 10th message to reduce UI freeze at scale.
        """

        def do_log():
            if dpg.does_item_exist("log_output"):
                self._log_lines.append(message)
                # Rebuild full text every 10 messages to avoid O(n) join on every append.
                # Skip intermediate updates — deque handles eviction, so the widget
                # stays in sync on the next full rebuild.
                # Rebuild full text every 10 messages or for important messages
                if (
                    len(self._log_lines) % 10 == 0
                    or len(self._log_lines) < 50
                    or "[Error]" in message
                    or "Done:" in message
                    or "SUCCESS" in message
                ):
                    dpg.set_value("log_output", "\n".join(self._log_lines))

        # If called from main thread, do directly; otherwise queue
        try:
            if threading.current_thread() is threading.main_thread():
                do_log()
            else:
                self._schedule_ui(do_log)
        except Exception as e:
            logger.exception("Log error: %s", e)

    # Import dialog
    def _show_import_dialog(self, sender=None, app_data=None) -> None:
        """Show file dialog to select sessions folder."""
        if "import_sessions" in self._busy_ops:
            self._log("[Import] Import already in progress")
            return
        self._busy_ops.add("import_sessions")
        try:
            # Use tkinter dialog (more stable than dpg file dialog)
            folder_path = _select_folder_tkinter()
            if folder_path is None:
                self._log("[Import] Cancelled")
                self._busy_ops.discard("import_sessions")
                return

            self._log(f"[Import] Scanning {folder_path}...")

            async def do_import():
                try:
                    imported, skipped = await self._controller.import_sessions(
                        folder_path, on_progress=lambda done, total, msg: self._log(f"[Import] {done}/{total} {msg}")
                    )
                    self._log(f"[Import] Done: {imported} imported, {skipped} skipped")

                    # Schedule UI updates on main thread
                    accounts = await self._controller.search_accounts("")
                    stats = await self._controller.get_stats()
                    proxies = await self._controller.db.list_proxies()
                    proxies_map = {p.id: p for p in proxies}

                    self._schedule_ui(lambda: self._update_stats_sync(stats))
                    self._schedule_ui(lambda: self._update_accounts_table_sync(accounts, proxies_map))

                except Exception as e:
                    logger.exception("Import error: %s", e)
                    self._log(f"[Error] Import failed: {_se(str(e))}")
                finally:
                    self._busy_ops.discard("import_sessions")

            self._run_async(do_import())

        except Exception as e:
            logger.exception("Import dialog error: %s", e)
            self._log(f"[Error] Import: {_se(str(e))}")
            self._busy_ops.discard("import_sessions")

    def _update_stats_sync(self, stats: dict) -> None:
        """Update stats on main thread."""
        try:
            dpg.set_value("stat_total", str(stats["total"]))
            dpg.set_value("stat_healthy", str(stats["healthy"]))
            dpg.set_value("stat_migrating", str(stats["migrating"]))
            dpg.set_value("stat_errors", str(stats["errors"]))
            dpg.set_value("stat_fragment", str(stats.get("fragment_authorized", 0)))
            dpg.set_value("stat_proxies", f"{stats['proxies_active']}/{stats['proxies_total']}")
        except Exception as e:
            logger.exception("Update stats error: %s", e)

    def _update_accounts_table_sync(self, accounts: list[AccountRecord], proxies_map: dict = None) -> None:
        """Update table on main thread (full rebuild)."""
        try:
            # Clear existing rows
            for child in dpg.get_item_children("accounts_table", 1) or []:
                dpg.delete_item(child)

            # Reset tracked widgets
            self._row_action_buttons.clear()
            self._status_cells.clear()
            self._fragment_cells.clear()

            # Empty state: show placeholder when no accounts loaded
            if not accounts:
                with dpg.table_row(parent="accounts_table"):
                    dpg.add_text("")  # checkbox column
                    dpg.add_text("Нет аккаунтов. Нажмите 'Import Sessions'")
                    for _ in range(5):  # remaining 5 columns
                        dpg.add_text("")
                return

            # Determine if batch buttons should be disabled (batch is running)
            batch_running = self._active_pool is not None

            # Add rows
            for account in accounts:
                with dpg.table_row(parent="accounts_table"):
                    # Checkbox
                    dpg.add_checkbox(tag=f"sel_{account.id}")

                    # Name
                    dpg.add_text(account.name)

                    # Username
                    dpg.add_text(account.username or "-")

                    # Status with color (Fix #11: track cell tag for incremental updates)
                    status_text = dpg.add_text(account.status)
                    self._status_cells[account.id] = status_text
                    if account.status in self._status_themes:
                        dpg.bind_item_theme(status_text, self._status_themes[account.status])

                    # Fragment status (Fix #11: track cell tag)
                    frag = account.fragment_status or "-"
                    frag_text = dpg.add_text(frag)
                    self._fragment_cells[account.id] = frag_text
                    if frag == "authorized":
                        dpg.bind_item_theme(frag_text, self._status_themes.get("healthy"))

                    # Proxy
                    if account.proxy_id and proxies_map and account.proxy_id in proxies_map:
                        proxy = proxies_map[account.proxy_id]
                        proxy_text = f"{proxy.host}:{proxy.port}"
                    elif account.proxy_id:
                        proxy_text = f"ID:{account.proxy_id}"
                    else:
                        proxy_text = "-"
                    dpg.add_text(proxy_text)

                    # Actions (Fix #9: track buttons for batch disable)
                    with dpg.group(horizontal=True):
                        btn_open = dpg.add_button(
                            label="Open", callback=self._open_profile, user_data=account.id, width=50
                        )
                        self._row_action_buttons.append(btn_open)
                        btn_migrate = dpg.add_button(
                            label="Migrate", callback=self._migrate_single, user_data=account.id, width=55
                        )
                        self._row_action_buttons.append(btn_migrate)
                        btn_proxy = dpg.add_button(
                            label="Proxy", callback=self._show_assign_proxy_dialog, user_data=account.id, width=45
                        )
                        self._row_action_buttons.append(btn_proxy)
                        btn_frag = dpg.add_button(
                            label="Frag", callback=self._fragment_single, user_data=account.id, width=40
                        )
                        self._row_action_buttons.append(btn_frag)

            # If a batch is running, keep per-row buttons disabled
            if batch_running:
                for btn_tag in self._row_action_buttons:
                    if dpg.does_item_exist(btn_tag):
                        dpg.configure_item(btn_tag, enabled=False)

        except Exception as e:
            logger.exception("Update accounts table error: %s", e)
            self._log(f"[Error] Table update: {_se(str(e))}")

    def _update_proxies_table_sync(self, proxies: list, accounts_map: dict = None) -> None:
        """Update proxies table on main thread."""
        try:
            # Clear existing rows
            for child in dpg.get_item_children("proxies_table", 1) or []:
                dpg.delete_item(child)

            # Add rows
            for proxy in proxies:
                with dpg.table_row(parent="proxies_table"):
                    # Host:Port
                    dpg.add_text(f"{proxy.host}:{proxy.port}")

                    # Protocol
                    dpg.add_text(proxy.protocol)

                    # Status with color
                    status_text = dpg.add_text(proxy.status)
                    if proxy.status == "active":
                        dpg.bind_item_theme(status_text, self._status_themes.get("healthy"))
                    elif proxy.status == "dead":
                        dpg.bind_item_theme(status_text, self._status_themes.get("error"))

                    # Assigned To - show account name if available
                    if proxy.assigned_account_id and accounts_map and proxy.assigned_account_id in accounts_map:
                        assigned = accounts_map[proxy.assigned_account_id].name
                    elif proxy.assigned_account_id:
                        assigned = f"#{proxy.assigned_account_id}"
                    else:
                        assigned = "-"
                    dpg.add_text(assigned)

                    # Actions
                    dpg.add_button(label="Delete", callback=self._delete_proxy, user_data=proxy.id, width=60)

            self._log(f"[UI] Loaded {len(proxies)} proxies")
        except Exception as e:
            logger.exception("Update proxies table error: %s", e)
            self._log(f"[Error] Proxies table: {_se(str(e))}")

    def _delete_proxy(self, sender, app_data, user_data) -> None:
        """Delete a proxy."""
        proxy_id = user_data

        async def do_delete():
            try:
                # Unlink any account that references this proxy before deletion
                # (prevents dangling proxy_id → account running without proxy)
                accounts = await self._controller.db.list_accounts()
                for acc in accounts:
                    if acc.proxy_id == proxy_id:
                        await self._controller.db.update_account(acc.id, proxy_id=None)
                await self._controller.db.delete_proxy(proxy_id)
                self._log(f"[Proxies] Deleted proxy {proxy_id}")

                # Refresh UI
                proxies = await self._controller.db.list_proxies()
                stats = await self._controller.get_stats()
                accounts = await self._controller.search_accounts("")
                accounts_map = {a.id: a for a in accounts}
                self._schedule_ui(lambda: self._update_proxies_table_sync(proxies, accounts_map))
                self._schedule_ui(lambda: self._update_stats_sync(stats))

            except Exception as e:
                logger.exception("Delete proxy error: %s", e)
                self._log(f"[Error] Delete proxy: {_se(str(e))}")

        self._run_async(do_delete())

    def _on_search_accounts(self, sender, filter_string) -> None:
        """Handle search input."""
        try:

            async def do_search():
                try:
                    accounts = await self._controller.search_accounts(filter_string)
                    proxies = await self._controller.db.list_proxies()
                    proxies_map = {p.id: p for p in proxies}
                    self._schedule_ui(lambda: self._update_accounts_table_sync(accounts, proxies_map))
                except Exception as e:
                    logger.exception("Search error: %s", e)
                    self._log(f"[Error] Search: {_se(str(e))}")

            self._run_async(do_search())
        except Exception as e:
            logger.exception("Search accounts error: %s", e)

    def _on_account_click(self, sender, app_data, user_data) -> None:
        """Handle account row click."""
        account_id = user_data
        self._log(f"[UI] Selected account {account_id}")

    def _open_profile(self, sender, app_data, user_data) -> None:
        """Open browser profile for account."""
        # Guard: prevent double-click launching two browsers for same profile
        if self._active_pool:
            self._log("[Open] Batch in progress, please wait")
            return
        op_key = f"open_{user_data}"
        if op_key in self._busy_ops:
            self._log("[Open] Already opening this profile...")
            return
        self._busy_ops.add(op_key)
        account_id = user_data
        self._log(f"[Open] Button clicked for account {account_id}")

        async def do_open():
            try:
                from ..browser_manager import BrowserManager
                from ..telegram_auth import AccountConfig

                account = await self._controller.db.get_account(account_id)
                if not account:
                    self._log(f"[Error] Account {account_id} not found")
                    return

                # Resolve profile name via AccountConfig (same logic as migration).
                # DB stores composite name "573189007843 (Kamila)" but profile
                # directory is created using AccountConfig.name ("Kamila").
                from ..paths import resolve_path

                session_path = resolve_path(account.session_path)
                account_dir = session_path.parent
                try:
                    acfg = AccountConfig.load(account_dir)
                    profile_name = acfg.name
                except Exception:
                    # Fallback to DB name if config is missing
                    profile_name = account.name
                self._log(f"[Open] Launching browser for {profile_name}...")

                # Use BrowserManager directly (not CLI!)
                manager = BrowserManager()
                profile = manager.get_profile(profile_name)

                if not profile.exists():
                    self._log(f"[Open] Profile not found: {profile_name}")
                    self._log("[Open] Hint: Run migration first to create browser profile")
                    return

                # Load proxy from profile config if exists
                if profile.config_path.exists():
                    import json

                    try:
                        with open(profile.config_path, encoding="utf-8") as f:
                            config = json.load(f)
                            profile.proxy = config.get("proxy")
                    except Exception as e:
                        logger.debug("Proxy config read failed for %s: %s", profile.config_path, e)

                # Launch browser - track manager for cleanup on failure
                ctx = None
                try:
                    ctx = await manager.launch(profile, headless=False)

                    # Track browser for cleanup on shutdown (schedule on main thread for thread safety)
                    _ctx = ctx
                    self._schedule_ui(lambda: self._active_browsers.append(_ctx))

                    page = await ctx.new_page()
                    await page.goto("https://web.telegram.org/k/")

                    self._log(f"[Open] Browser opened for {profile_name}")
                    # Browser runs in background - will be cleaned up on app shutdown
                except Exception:
                    # P1 FIX: Clean up BrowserManager if launch/page fails
                    await manager.close_all()
                    raise
            except Exception as e:
                logger.exception("Open profile error: %s", e)
                self._log(f"[Error] Open: {_se(str(e))}")
            finally:
                self._busy_ops.discard(op_key)

        self._run_async(do_open())

    def _migrate_single(self, sender, app_data, user_data) -> None:
        """Migrate single account."""
        # Fix #9: Guard against concurrent session use during batch ops
        if self._active_pool:
            self._log("[Migrate] Batch in progress, please wait")
            return
        # Guard against double-click on same account
        op_key = f"migrate_{user_data}"
        if op_key in self._busy_ops:
            self._log("[Migrate] Already migrating this account")
            return
        self._busy_ops.add(op_key)
        account_id = user_data
        self._log(f"[Migrate] Button clicked for account {account_id}")

        async def do_migrate():
            try:
                from ..telegram_auth import migrate_account

                account = await self._controller.db.get_account(account_id)
                if not account:
                    self._log(f"[Error] Account {account_id} not found")
                    return

                self._log(f"[Migrate] Starting {account.name}...")

                # Update status immediately
                await self._controller.db.update_account(account_id, status="migrating")
                self._schedule_ui(lambda: self._refresh_table_async())

                # Get session directory from database path
                from ..paths import resolve_path

                session_path = resolve_path(account.session_path)
                session_dir = session_path.parent

                if not session_dir.exists():
                    self._log(f"[Error] Session dir not found: {session_dir}")
                    await self._controller.db.update_account(
                        account_id, status="error", error_message="Session dir not found"
                    )
                    self._schedule_ui(lambda: self._refresh_table_async())
                    return

                # Build proxy string from DB if available
                proxy_str = await self._build_proxy_string(account)

                if proxy_str is None and account.proxy_id is None:
                    self._log(f"[Warning] {account.name}: прокси не назначен — миграция пойдёт с реальным IP!")

                # Call migrate_account directly (not via CLI!)
                try:
                    result = await migrate_account(
                        account_dir=session_dir,
                        password_2fa=self._2fa_password,
                        headless=True,  # GUI mode uses headless by default
                        proxy_override=proxy_str,
                    )

                    if result.success:
                        from datetime import datetime

                        await self._controller.db.update_account(
                            account_id,
                            status="healthy",
                            username=result.user_info.get("username") if result.user_info else None,
                            error_message=None,
                            web_last_verified=datetime.now(UTC).isoformat(),
                            auth_ttl_days=365,
                        )
                        self._log(f"[Migrate] {account.name} - SUCCESS")
                        if result.user_info:
                            self._log(f"[Migrate] Username: @{result.user_info.get('username', 'N/A')}")
                    else:
                        await self._controller.db.update_account(account_id, status="error", error_message=_se(result.error))
                        self._log(f"[Migrate] {account.name} - FAILED: {_se(result.error)}")

                except Exception as e:
                    from ..utils import sanitize_error as _sanitize

                    logger.exception("Migration error for %s: %s", account.name, _sanitize(str(e)))
                    await self._controller.db.update_account(
                        account_id, status="error", error_message=_sanitize(str(e))
                    )
                    self._log(f"[Migrate] {account.name} - ERROR: {_sanitize(str(e))}")

                self._schedule_ui(lambda: self._refresh_table_async())

            except Exception as e:
                logger.exception("Migrate error: %s", e)
                self._log(f"[Error] Migrate: {_se(str(e))}")
                try:
                    await self._controller.db.update_account(account_id, status="error")
                except Exception as db_err:
                    logger.warning("Failed to mark account %d as error in DB: %s", account_id, db_err)
                self._schedule_ui(lambda: self._refresh_table_async())
            finally:
                self._busy_ops.discard(op_key)

        self._run_async(do_migrate())

    def _fragment_single(self, sender, app_data, user_data) -> None:
        """Authorize single account on fragment.com."""
        # Fix #9: Guard against concurrent session use during batch ops
        if self._active_pool:
            self._log("[Fragment] Batch in progress, please wait")
            return
        # Guard against double-click on same account
        op_key = f"fragment_{user_data}"
        if op_key in self._busy_ops:
            self._log("[Fragment] Already processing this account")
            return
        self._busy_ops.add(op_key)
        account_id = user_data
        self._log(f"[Fragment] Button clicked for account {account_id}")

        async def do_fragment():
            try:
                account = await self._controller.db.get_account(account_id)
                if not account:
                    self._log(f"[Fragment] Account {account_id} not found")
                    return

                from ..paths import resolve_path as _resolve_path

                session_path = _resolve_path(account.session_path) if account.session_path else None
                if not session_path:
                    self._log(f"[Fragment] Session not found for {account.name}")
                    return

                session_dir = session_path.parent
                if not session_dir.exists():
                    self._log(f"[Fragment] Session dir not found: {session_dir}")
                    return

                self._log(f"[Fragment] Starting {account.name}...")

                from ..browser_manager import BrowserManager
                from ..fragment_auth import FragmentAuth
                from ..telegram_auth import AccountConfig

                try:
                    config = AccountConfig.load(session_dir)
                except Exception as e:
                    self._log(f"[Fragment] Config load error: {_se(str(e))}")
                    return

                # Apply proxy from DB
                proxy_str = await self._build_proxy_string(account)
                if proxy_str:
                    config.proxy = proxy_str

                # Fix #3: BrowserManager must be cleaned up in finally
                browser_manager = BrowserManager()
                try:
                    auth = FragmentAuth(config, browser_manager)
                    result = await asyncio.wait_for(auth.connect(headless=True), timeout=300)

                    if result.success:
                        from datetime import datetime

                        status = "already authorized" if result.already_authorized else "connected"
                        self._log(f"[Fragment] {account.name} - {status}")
                        await self._controller.db.update_account(
                            account_id,
                            fragment_status="authorized",
                            web_last_verified=datetime.now(UTC).isoformat(),
                        )
                    else:
                        self._log(f"[Fragment] {account.name} - FAILED: {_se(result.error)}")
                        await self._controller.db.update_account(
                            account_id, fragment_status="error", error_message=_se(result.error)
                        )
                finally:
                    await browser_manager.close_all()

                self._schedule_ui(lambda: self._refresh_table_async())

            except Exception as e:
                logger.exception("Fragment error for %s: %s", account.name, e)
                self._log(f"[Error] Fragment: {_se(str(e))}")
                try:
                    await self._controller.db.update_account(
                        account_id, fragment_status="error", error_message=_se(str(e))
                    )
                except Exception as db_err:
                    logger.warning("Failed to mark fragment_status=error for %d: %s", account_id, db_err)
            finally:
                self._busy_ops.discard(op_key)

        self._run_async(do_fragment())

    async def _build_proxy_string(self, account: AccountRecord) -> str | None:
        """Build proxy string from DB ProxyRecord for migrate_account()."""
        if not account.proxy_id:
            return None
        proxy = await self._controller.db.get_proxy(account.proxy_id)
        if not proxy:
            return None
        if proxy.status == "dead":
            self._log(f"[Warning] Proxy {proxy.host}:{proxy.port} is dead for {account.name}")
            return None
        # Format: socks5:host:port:user:pass or socks5:host:port
        if proxy.username and proxy.password:
            return f"{proxy.protocol}:{proxy.host}:{proxy.port}:{proxy.username}:{proxy.password}"
        return f"{proxy.protocol}:{proxy.host}:{proxy.port}"

    def _update_status_cells(self, accounts: list[AccountRecord]) -> None:
        """Incrementally update only status and fragment cells (Fix #11).

        During batch ops this avoids destroying/recreating all 1000 rows
        (14K DPG widget ops) every 3 seconds. Only changed cells are touched.
        """
        try:
            for account in accounts:
                # Update status cell
                cell_tag = self._status_cells.get(account.id)
                if cell_tag and dpg.does_item_exist(cell_tag):
                    dpg.set_value(cell_tag, account.status)
                    if account.status in self._status_themes:
                        dpg.bind_item_theme(cell_tag, self._status_themes[account.status])

                # Update fragment cell
                frag_tag = self._fragment_cells.get(account.id)
                if frag_tag and dpg.does_item_exist(frag_tag):
                    frag = account.fragment_status or "-"
                    dpg.set_value(frag_tag, frag)
                    if frag == "authorized":
                        dpg.bind_item_theme(frag_tag, self._status_themes.get("healthy"))
        except Exception as e:
            logger.exception("Update status cells error: %s", e)

    def _refresh_table_async(self) -> None:
        """Trigger async table refresh from UI thread.

        Fix #11: During batch ops (self._active_pool set), only update
        status/fragment cells incrementally instead of full rebuild.
        """

        async def do_refresh():
            accounts = await self._controller.search_accounts("")
            stats = await self._controller.get_stats()
            self._schedule_ui(lambda: self._update_stats_sync(stats))
            if self._active_pool and self._status_cells:
                # Incremental update during batch — no full rebuild
                self._schedule_ui(lambda: self._update_status_cells(accounts))
                # Ensure per-row buttons stay disabled during batch
                self._schedule_ui(lambda: self._enforce_batch_button_state())
            else:
                # Full rebuild when no batch is running
                proxies = await self._controller.db.list_proxies()
                proxies_map = {p.id: p for p in proxies}
                self._schedule_ui(lambda: self._update_accounts_table_sync(accounts, proxies_map))

        self._run_async(do_refresh())

    def _enforce_batch_button_state(self) -> None:
        """Re-disable per-row action buttons during active batch operations."""
        if self._active_pool:
            toggled = 0
            for btn_tag in self._row_action_buttons:
                if dpg.does_item_exist(btn_tag):
                    dpg.configure_item(btn_tag, enabled=False)
                    toggled += 1
            logger.debug("_enforce_batch_button_state: %d buttons disabled", toggled)

    def _maybe_periodic_refresh(self) -> None:
        """Periodic table refresh during batch ops (every 10s).

        Ensures GUI shows 'migrating' status between on_progress callbacks,
        which only fire when accounts complete (can take 5+ minutes).
        """
        if not self._active_pool or self._active_pool == "pending":
            return
        now = time.monotonic()
        if now - self._last_table_refresh >= 10.0:
            self._last_table_refresh = now
            self._refresh_table_async()

    def _show_assign_proxy_dialog(self, sender, app_data, user_data) -> None:
        """Show dialog to assign proxy to account."""
        account_id = user_data
        self._log(f"[Proxy] Opening dialog for account {account_id}...")

        async def show_dialog():
            try:
                account = await self._controller.db.get_account(account_id)
                proxies = await self._controller.db.list_proxies()

                # Build proxy options - schedule on main thread
                self._schedule_ui(lambda: self._create_proxy_dialog(account, proxies))

            except Exception as e:
                logger.exception("Show assign proxy dialog error: %s", e)
                self._log(f"[Error] Proxy dialog: {_se(str(e))}")

        self._run_async(show_dialog())

    def _create_proxy_dialog(self, account, proxies) -> None:
        """Create proxy selection dialog on main thread."""
        dialog_tag = f"proxy_dialog_{account.id}"

        if dpg.does_item_exist(dialog_tag):
            dpg.delete_item(dialog_tag)

        with dpg.window(
            tag=dialog_tag, label=f"Assign Proxy: {account.name}", modal=True, width=400, height=300, pos=[400, 200]
        ):
            dpg.add_text(f"Select proxy for: {account.name}")
            dpg.add_separator()

            # Current proxy
            if account.proxy_id:
                dpg.add_text(f"Current: ID {account.proxy_id}", color=(150, 150, 150))
            else:
                dpg.add_text("Current: None", color=(150, 150, 150))

            dpg.add_spacer(height=10)

            # Proxy list
            with dpg.child_window(height=150):
                # None option
                dpg.add_selectable(
                    label="[No Proxy]", callback=self._on_proxy_selected, user_data=(account.id, None, dialog_tag)
                )

                for proxy in proxies:
                    status_icon = "[OK]" if proxy.status == "active" else "[X]"
                    assigned = " (used)" if proxy.assigned_account_id else ""
                    label = f"{status_icon} {proxy.host}:{proxy.port}{assigned}"

                    dpg.add_selectable(
                        label=label, callback=self._on_proxy_selected, user_data=(account.id, proxy.id, dialog_tag)
                    )

            dpg.add_spacer(height=10)
            dpg.add_button(
                label="Cancel",
                callback=lambda s, a, dt: dpg.delete_item(dt) if dpg.does_item_exist(dt) else None,
                user_data=dialog_tag,
                width=100,
            )

    def _on_proxy_selected(self, sender, app_data, user_data) -> None:
        """Handle proxy selection."""
        account_id, proxy_id, dialog_tag = user_data

        async def do_assign():
            try:
                if proxy_id is None:
                    # Remove proxy assignment
                    await self._controller.db.update_account(account_id, proxy_id=None)
                    self._log(f"[Proxies] Removed proxy from account {account_id}")
                else:
                    await self._controller.db.assign_proxy(account_id, proxy_id)
                    proxy = await self._controller.db.get_proxy(proxy_id)
                    self._log(f"[Proxies] Assigned {proxy.host}:{proxy.port} to account {account_id}")

                # Close dialog and refresh
                self._schedule_ui(lambda: dpg.delete_item(dialog_tag) if dpg.does_item_exist(dialog_tag) else None)
                self._schedule_ui(lambda: self._refresh_table_async())

            except Exception as e:
                logger.exception("Assign proxy error: %s", e)
                self._log(f"[Error] Assign: {_se(str(e))}")

        self._run_async(do_assign())

    def _migrate_selected(self, sender=None, app_data=None) -> None:
        """Migrate all selected accounts."""
        if self._active_pool:
            self._log("[Migrate] Migration already in progress")
            return
        # P0 FIX: Set sentinel BEFORE checkbox parsing to prevent double-click race
        self._active_pool = "pending"
        self._set_batch_buttons_enabled(False)
        try:
            # Get selected checkboxes
            selected_ids = []
            for child in dpg.get_item_children("accounts_table", 1) or []:
                row_children = dpg.get_item_children(child, 1) or []
                if row_children:
                    checkbox = row_children[0]
                    if dpg.get_value(checkbox):
                        tag = dpg.get_item_alias(checkbox)
                        if tag and tag.startswith("sel_"):
                            try:
                                selected_ids.append(int(tag[4:]))
                            except (ValueError, TypeError):
                                logger.warning("Skipping malformed checkbox tag: %s", tag)

            if not selected_ids:
                self._log("[Warning] No accounts selected")
                self._active_pool = None
                self._set_batch_buttons_enabled(True)
                return
            self._log(f"[Migrate] Starting migration of {len(selected_ids)} selected accounts...")

            self._run_async(self._batch_migrate(selected_ids))
        except Exception as e:
            logger.exception("Migrate selected error: %s", e)
            self._log(f"[Error] Migrate: {_se(str(e))}")
            self._active_pool = None
            self._set_batch_buttons_enabled(True)

    def _migrate_all(self, sender=None, app_data=None) -> None:
        """Migrate all pending accounts."""
        # FIX-F: Guard against double-click creating orphaned pool → OOM
        if self._active_pool:
            self._log("[Migrate] Migration already in progress")
            return
        # Disable buttons immediately on main thread to prevent race
        self._set_batch_buttons_enabled(False)
        # P0 FIX: Set sentinel BEFORE _run_async to prevent double-click race
        self._active_pool = "pending"
        self._log("[Migrate] Starting batch migration of all pending...")

        async def get_pending_ids():
            try:
                accounts = await self._controller.db.list_accounts(status="pending")
                if not accounts:
                    self._log("[Migrate] No pending accounts")
                    self._active_pool = None
                    self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                    return
                ids = [a.id for a in accounts]
                self._log(f"[Migrate] {len(ids)} accounts to migrate...")
                await self._batch_migrate(ids)
            except Exception as e:
                logger.exception("get_pending_ids error: %s", e)
                self._log(f"[Error] get_pending_ids: {_se(str(e))}")
                self._active_pool = None
                self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                self._schedule_ui(lambda: self._reset_stop_button())

        self._run_async(get_pending_ids())

    def _set_batch_buttons_enabled(self, enabled: bool) -> None:
        """Enable/disable batch AND per-row action buttons to prevent double-click.

        Fix #9: Also toggles per-row Migrate/Frag/Open/Proxy buttons so users
        cannot trigger concurrent session use during batch operations.
        """
        for tag in ("btn_migrate_selected", "btn_migrate_all", "btn_retry_failed", "btn_fragment_all"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)
        # Fix #9: Toggle per-row action buttons
        toggled = 0
        for btn_tag in self._row_action_buttons:
            if dpg.does_item_exist(btn_tag):
                dpg.configure_item(btn_tag, enabled=enabled)
                toggled += 1
        logger.debug("_set_batch_buttons_enabled(%s): %d per-row buttons toggled", enabled, toggled)

    def _retry_failed(self, sender=None, app_data=None) -> None:
        """Retry all accounts with status='error'."""
        if self._active_pool:
            self._log("[Retry] Migration already in progress")
            return
        # Disable buttons immediately on main thread
        self._set_batch_buttons_enabled(False)
        # P0 FIX: Set sentinel BEFORE _run_async to prevent double-click race
        self._active_pool = "pending"
        self._log("[Retry] Retrying failed accounts...")

        async def get_error_ids():
            try:
                accounts = await self._controller.db.list_accounts(status="error")
                if not accounts:
                    self._log("[Retry] Нет аккаунтов с ошибками")
                    self._active_pool = None
                    self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                    return
                # Reset status to pending for retry
                for a in accounts:
                    await self._controller.db.update_account(a.id, status="pending", error_message=None)
                ids = [a.id for a in accounts]
                self._log(f"[Retry] {len(ids)} аккаунтов на повтор...")
                await self._batch_migrate(ids)
            except Exception as e:
                logger.exception("get_error_ids error: %s", e)
                self._log(f"[Error] get_error_ids: {_se(str(e))}")
                self._active_pool = None
                self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                self._schedule_ui(lambda: self._reset_stop_button())

        self._run_async(get_error_ids())

    def _stop_migration(self, sender=None, app_data=None) -> None:
        """Stop ongoing migration."""
        # Ensure _active_pool is a real pool object (not "pending" sentinel or None)
        if not (self._active_pool and hasattr(self._active_pool, "request_shutdown")):
            return
        # Disable immediately on main thread (not via _schedule_ui) to prevent double-click
        if dpg.does_item_exist("btn_stop"):
            dpg.configure_item("btn_stop", enabled=False, label="Stopping...")
        self._active_pool.request_shutdown()
        self._log("[Migrate] STOP requested - finishing active accounts...")

    def _disable_stop_button(self) -> None:
        """Disable STOP button and change label to show stopping state."""
        if dpg.does_item_exist("btn_stop"):
            dpg.configure_item("btn_stop", enabled=False, label="Stopping...")

    def _reset_stop_button(self) -> None:
        """Re-enable STOP button and restore label after batch completes."""
        if dpg.does_item_exist("btn_stop"):
            dpg.configure_item("btn_stop", enabled=True, label="STOP")

    def _throttled_refresh(self, completed: int, total: int, force: bool = False) -> None:
        """Refresh accounts table at most every 3s (last update always fires)."""
        now = time.monotonic()
        if force or completed >= total or (now - self._last_table_refresh >= 3.0):
            self._last_table_refresh = now
            self._schedule_ui(lambda: self._refresh_table_async())


    async def _preflight_check_proxies(self, account_ids: list[int], mode: str = "Migrate") -> bool:
        """Pre-flight check: ensure all batch accounts have live proxies.

        Args:
            account_ids: IDs of accounts to check.
            mode: "Migrate" or "Fragment" for log prefix.

        Returns:
            True if all clear, False if batch should be aborted.
        """
        all_accounts = await self._controller.db.list_accounts()
        all_proxies = await self._controller.db.list_proxies()
        proxy_map = {p.id: p for p in all_proxies}

        batch_set = set(account_ids)
        batch_accounts = [a for a in all_accounts if a.id in batch_set]

        no_proxy: list[str] = []
        dead_proxy: list[str] = []

        for a in batch_accounts:
            if a.proxy_id is None:
                no_proxy.append(a.name)
            elif a.proxy_id in proxy_map:
                if proxy_map[a.proxy_id].status == "dead":
                    dead_proxy.append(a.name)
            else:
                # proxy_id references missing proxy record
                no_proxy.append(a.name)

        if not no_proxy and not dead_proxy:
            return True

        if no_proxy:
            names_preview = ", ".join(no_proxy[:5])
            suffix = f" и ещё {len(no_proxy) - 5}" if len(no_proxy) > 5 else ""
            self._log(
                f"[{mode}] ОСТАНОВЛЕНО: {len(no_proxy)} аккаунтов без прокси: {names_preview}{suffix}"
            )

        if dead_proxy:
            names_preview = ", ".join(dead_proxy[:5])
            suffix = f" и ещё {len(dead_proxy) - 5}" if len(dead_proxy) > 5 else ""
            self._log(
                f"[{mode}] ОСТАНОВЛЕНО: {len(dead_proxy)} аккаунтов с мёртвым прокси: {names_preview}{suffix}"
            )

        self._log(f"[{mode}] Нажмите 'Auto-Assign Proxies' или импортируйте прокси перед запуском")
        return False

    async def _quick_auto_assign(self, account_ids: list[int], mode: str = "Migrate") -> int:
        """Quick auto-assign free proxies to batch accounts without proxies.

        Unlike _auto_assign_proxies (button handler), this is a lightweight
        pre-batch step: no UI refresh, no dead-proxy unlinking.

        Args:
            account_ids: IDs of accounts to check and assign.
            mode: "Migrate" or "Fragment" for log prefix.

        Returns:
            Number of proxies assigned.
        """
        all_accounts = await self._controller.db.list_accounts()
        batch_set = set(account_ids)

        needs_proxy = [a for a in all_accounts if a.id in batch_set and a.proxy_id is None]
        if not needs_proxy:
            return 0

        assigned = 0
        for a in needs_proxy:
            proxy = await self._controller.db.get_free_proxy()
            if not proxy:
                break  # No more free proxies available
            try:
                await self._controller.db.assign_proxy(a.id, proxy.id)
                assigned += 1
                self._log(f"[{mode}] Авто-назначен прокси: {a.name} <- {proxy.host}:{proxy.port}")
            except ValueError:
                continue  # Proxy race — skip

        return assigned

    async def _batch_migrate(self, account_ids: list) -> None:
        """Batch migrate accounts using parallel worker pool."""
        # FIX: Filter out accounts with active single-migrate/fragment ops
        # to prevent AUTH_KEY_DUPLICATED from concurrent session access
        busy_ids = {
            int(op.split("_", 1)[1])
            for op in self._busy_ops
            if op.startswith(("migrate_", "fragment_")) and op.split("_", 1)[1].isdigit()
        }
        if busy_ids:
            original_count = len(account_ids)
            account_ids = [aid for aid in account_ids if aid not in busy_ids]
            if original_count != len(account_ids):
                self._log(f"[Migrate] Skipped {original_count - len(account_ids)} accounts with active single ops")
        if not account_ids:
            self._log("[Migrate] No accounts to migrate")
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            return

        # Auto-assign free proxies to accounts without them
        auto_count = await self._quick_auto_assign(account_ids, mode="Migrate")
        if auto_count > 0:
            self._log(f"[Migrate] Автоматически назначено {auto_count} прокси")

        # Pre-flight: verify all accounts have live proxies
        preflight_ok = await self._preflight_check_proxies(account_ids, mode="Migrate")
        if not preflight_ok:
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            return

        # Canary warning for large batches
        if len(account_ids) > 100:
            self._log(
                f"[Migrate] Внимание: {len(account_ids)} аккаунтов в батче. "
                "Рекомендуется сначала запустить пробный прогон на 50 аккаунтах "
                "через 'Migrate Selected', чтобы убедиться что всё работает."
            )

        self._schedule_ui(lambda: self._set_batch_buttons_enabled(False))
        try:
            from ..resource_monitor import ResourceMonitor
            from ..worker_pool import MigrationWorkerPool

            monitor = ResourceMonitor()
            num_workers = min(monitor.recommended_concurrency(), 5)
            self._log(f"[Migrate] Воркеров: {num_workers} (RAM: {monitor.get_current()['memory_available_gb']:.1f}GB свободно)")

            pool = MigrationWorkerPool(
                db=self._controller.db,
                num_workers=num_workers,
                batch_pause_every=50,
                password_2fa=self._2fa_password,
                resource_monitor=monitor,
                on_progress=lambda completed, total, result: self._throttled_refresh(completed, total),
                on_log=lambda msg: self._log(msg),
            )

            self._active_pool = pool

            # Fix C6: Ensure _status_cells is populated before batch starts
            # so that _throttled_refresh can do incremental updates
            if not self._status_cells:
                accounts_list = await self._controller.search_accounts("")
                proxies_list = await self._controller.db.list_proxies()
                proxies_map = {p.id: p for p in proxies_list}
                self._schedule_ui(lambda: self._update_accounts_table_sync(accounts_list, proxies_map))
                await asyncio.sleep(0.1)

            result = await pool.run(account_ids)

            self._log(f"[Migrate] Done: {result.success_count} OK, {result.error_count} errors, {result.total} total")
            self._schedule_ui(lambda: self._refresh_table_async())

        except BaseException as e:
            logger.exception("Batch migrate error: %s", e)
            self._log(f"[Error] Batch migrate: {_se(str(e))}")
        finally:
            # P0 FIX: Always reset pool in finally for clean state
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            self._schedule_ui(lambda: self._reset_stop_button())

    def _fragment_all(self, sender=None, app_data=None) -> None:
        """Start fragment.com auth for all healthy (migrated) accounts."""
        if self._active_pool:
            self._log("[Fragment] Migration already in progress")
            return

        # Disable buttons immediately on main thread
        self._set_batch_buttons_enabled(False)
        # P0 FIX: Set sentinel BEFORE _run_async to prevent double-click race
        self._active_pool = "pending"
        self._log("[Fragment] Starting batch fragment auth for healthy accounts...")

        async def get_healthy_ids():
            try:
                accounts = await self._controller.db.list_accounts(status="healthy")
                # Skip already fragment-authorized accounts
                accounts = [a for a in accounts if a.fragment_status != "authorized"]
                if not accounts:
                    self._log("[Fragment] Нет аккаунтов для авторизации (все уже авторизованы или нет мигрированных)")
                    self._active_pool = None
                    self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                    return
                ids = [a.id for a in accounts]
                self._log(f"[Fragment] {len(ids)} аккаунтов для авторизации на fragment.com...")
                await self._batch_fragment(ids)
            except Exception as e:
                logger.exception("get_healthy_ids error: %s", e)
                self._log(f"[Error] get_healthy_ids: {_se(str(e))}")
                self._active_pool = None
                self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
                self._schedule_ui(lambda: self._reset_stop_button())

        self._run_async(get_healthy_ids())

    async def _batch_fragment(self, account_ids: list) -> None:
        """Batch fragment auth using parallel worker pool."""
        # FIX: Filter out accounts with active single ops (same as _batch_migrate)
        busy_ids = {
            int(op.split("_", 1)[1])
            for op in self._busy_ops
            if op.startswith(("migrate_", "fragment_")) and op.split("_", 1)[1].isdigit()
        }
        if busy_ids:
            original_count = len(account_ids)
            account_ids = [aid for aid in account_ids if aid not in busy_ids]
            if original_count != len(account_ids):
                self._log(f"[Fragment] Skipped {original_count - len(account_ids)} accounts with active single ops")
        if not account_ids:
            self._log("[Fragment] No accounts to process")
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            return

        # Auto-assign free proxies to accounts without them
        auto_count = await self._quick_auto_assign(account_ids, mode="Fragment")
        if auto_count > 0:
            self._log(f"[Fragment] Автоматически назначено {auto_count} прокси")

        # Pre-flight: verify all accounts have live proxies
        preflight_ok = await self._preflight_check_proxies(account_ids, mode="Fragment")
        if not preflight_ok:
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            return

        self._schedule_ui(lambda: self._set_batch_buttons_enabled(False))
        try:
            from ..resource_monitor import ResourceMonitor
            from ..worker_pool import MigrationWorkerPool

            monitor = ResourceMonitor()
            num_workers = min(monitor.recommended_concurrency(), 5)
            self._log(f"[Fragment] Воркеров: {num_workers} (RAM: {monitor.get_current()['memory_available_gb']:.1f}GB свободно)")

            pool = MigrationWorkerPool(
                db=self._controller.db,
                num_workers=num_workers,
                batch_pause_every=50,
                password_2fa=self._2fa_password,
                resource_monitor=monitor,
                on_progress=lambda completed, total, result: self._throttled_refresh(completed, total),
                on_log=lambda msg: self._log(msg),
                mode="fragment",
            )

            self._active_pool = pool

            # Fix C6: Ensure _status_cells is populated before batch starts
            # so that _throttled_refresh can do incremental updates
            if not self._status_cells:
                accounts_list = await self._controller.search_accounts("")
                proxies_list = await self._controller.db.list_proxies()
                proxies_map = {p.id: p for p in proxies_list}
                self._schedule_ui(lambda: self._update_accounts_table_sync(accounts_list, proxies_map))
                await asyncio.sleep(0.1)

            result = await pool.run(account_ids)

            self._log(f"[Fragment] Done: {result.success_count} OK, {result.error_count} errors, {result.total} total")
            self._schedule_ui(lambda: self._refresh_table_async())

        except BaseException as e:
            logger.exception("Batch fragment error: %s", e)
            self._log(f"[Error] Batch fragment: {_se(str(e))}")
        finally:
            # P0 FIX: Always reset pool in finally for clean state
            self._active_pool = None
            self._schedule_ui(lambda: self._set_batch_buttons_enabled(True))
            self._schedule_ui(lambda: self._reset_stop_button())

    def _auto_assign_proxies(self, sender=None, app_data=None) -> None:
        """Auto-assign free proxies to accounts without proxies or with dead proxies."""
        if "auto_assign" in self._busy_ops:
            self._log("[Proxies] Auto-assign already in progress")
            return
        self._busy_ops.add("auto_assign")
        self._log("[Proxies] Auto-assigning...")

        async def do_assign():
            try:
                all_accounts = await self._controller.db.list_accounts()
                all_proxies = await self._controller.db.list_proxies()
                proxy_map = {p.id: p for p in all_proxies}

                # Accounts needing proxy: no proxy OR dead proxy
                needs_proxy = []
                for a in all_accounts:
                    if a.proxy_id is None:
                        needs_proxy.append((a, "нет прокси"))
                    elif a.proxy_id in proxy_map:
                        p = proxy_map[a.proxy_id]
                        if p.status == "dead":
                            needs_proxy.append((a, f"dead прокси {p.host}:{p.port}"))

                if not needs_proxy:
                    self._log("[Proxies] Все аккаунты имеют рабочие прокси")
                    return

                self._log(f"[Proxies] Нужно назначить прокси: {len(needs_proxy)} аккаунтов")

                assigned = 0
                for account, reason in needs_proxy:
                    # Unlink old dead proxy first
                    if account.proxy_id is not None:
                        old_proxy = proxy_map.get(account.proxy_id)
                        # Unassign dead proxy from account
                        await self._controller.db.update_account(account.id, proxy_id=None)
                        if old_proxy:
                            await self._controller.db.update_proxy(
                                old_proxy.id, assigned_account_id=None
                            )

                    # Get free active proxy
                    proxy = await self._controller.db.get_free_proxy()
                    if not proxy:
                        self._log(f"[Proxies] Нет свободных прокси ({assigned} назначено)")
                        break

                    try:
                        await self._controller.db.assign_proxy(account.id, proxy.id)
                    except ValueError as ve:
                        self._log(f"[Proxies] {account.name}: конфликт, пропуск ({ve})")
                        continue
                    assigned += 1
                    self._log(
                        f"[Proxies] {account.name} <- {proxy.host}:{proxy.port} ({reason})"
                    )

                self._log(f"[Proxies] Назначено {assigned} прокси")

                # Refresh UI
                accounts = await self._controller.search_accounts("")
                stats = await self._controller.get_stats()
                proxies_list = await self._controller.db.list_proxies()
                proxies_ui_map = {p.id: p for p in proxies_list}
                self._schedule_ui(lambda: self._update_stats_sync(stats))
                self._schedule_ui(
                    lambda: self._update_accounts_table_sync(accounts, proxies_ui_map)
                )

            except Exception as e:
                logger.exception("Auto-assign error: %s", e)
                self._log(f"[Error] Auto-assign: {_se(str(e))}")
            finally:
                self._busy_ops.discard("auto_assign")

        self._run_async(do_assign())

    def _show_proxy_import_dialog(self, sender=None, app_data=None) -> None:
        """Show proxy import - select file directly."""
        if "import_proxies" in self._busy_ops:
            self._log("[Import] Proxy import already in progress")
            return
        self._busy_ops.add("import_proxies")
        try:
            filepath = _select_file_tkinter(
                title="Select Proxies File", filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )

            if filepath is None:
                self._log("[Import] Proxy import cancelled")
                self._busy_ops.discard("import_proxies")
                return

            self._log(f"[Import] Loading proxies from {filepath.name}...")

            # Read file
            proxy_text = filepath.read_text(encoding="utf-8", errors="ignore")

            async def do_import():
                try:
                    count = await self._controller.import_proxies(proxy_text)
                    self._log(f"[Import] Imported {count} proxies")

                    # Refresh UI
                    stats = await self._controller.get_stats()
                    proxies = await self._controller.db.list_proxies()
                    accounts = await self._controller.search_accounts("")
                    accounts_map = {a.id: a for a in accounts}

                    self._schedule_ui(lambda: self._update_stats_sync(stats))
                    self._schedule_ui(lambda: self._update_proxies_table_sync(proxies, accounts_map))
                except Exception as e:
                    logger.exception("Proxy import error: %s", e)
                    self._log(f"[Error] Proxy import: {_se(str(e))}")
                finally:
                    self._busy_ops.discard("import_proxies")

            self._run_async(do_import())

        except Exception as e:
            logger.exception("Show proxy import dialog error: %s", e)
            self._log(f"[Error] Proxy import: {_se(str(e))}")
            self._busy_ops.discard("import_proxies")

    def _check_all_proxies(self, sender=None, app_data=None) -> None:
        """Check all proxies status."""
        if "check_proxies" in self._busy_ops:
            self._log("[Proxies] Check already in progress")
            return
        self._busy_ops.add("check_proxies")
        self._log("[Proxies] Starting proxy check...")

        async def do_check():
            try:
                proxies = await self._controller.db.list_proxies()
                total = len(proxies)
                self._log(f"[Proxies] Checking {total} proxies (50 concurrent)...")

                alive = 0
                dead = 0
                sem = asyncio.Semaphore(50)

                async def check_one(proxy):
                    async with sem:
                        return proxy, await self._controller.check_proxy(proxy)

                results = await asyncio.gather(*(check_one(p) for p in proxies), return_exceptions=True)

                for result in results:
                    if isinstance(result, BaseException):
                        dead += 1
                        continue
                    proxy, is_alive = result
                    if is_alive:
                        alive += 1
                        await self._controller.db.update_proxy(proxy.id, status="active")
                    else:
                        dead += 1
                        await self._controller.db.update_proxy(proxy.id, status="dead")

                self._log(f"[Proxies] Done: {alive} alive, {dead} dead")

                # Refresh UI
                proxies = await self._controller.db.list_proxies()
                stats = await self._controller.get_stats()
                accounts = await self._controller.search_accounts("")
                accounts_map = {a.id: a for a in accounts}
                self._schedule_ui(lambda: self._update_proxies_table_sync(proxies, accounts_map))
                self._schedule_ui(lambda: self._update_stats_sync(stats))

            except Exception as e:
                logger.exception("Check proxies error: %s", e)
                self._log(f"[Error] Proxy check: {_se(str(e))}")
            finally:
                self._busy_ops.discard("check_proxies")

        self._run_async(do_check())

    def _replace_dead_proxies(self, sender=None, app_data=None) -> None:
        """Delete dead proxies from database, unlinking accounts first."""
        if "replace_dead" in self._busy_ops:
            self._log("[Proxies] Replace already in progress")
            return
        self._busy_ops.add("replace_dead")

        async def do_replace():
            try:
                proxies = await self._controller.db.list_proxies(status="dead")
                if not proxies:
                    self._log("[Proxies] No dead proxies to remove")
                    return

                count = 0
                for proxy in proxies:
                    # Unlink any account that references this proxy
                    if proxy.assigned_account_id:
                        await self._controller.db.update_account(proxy.assigned_account_id, proxy_id=None)
                        self._log(
                            f"[Proxies] Unlinked account {proxy.assigned_account_id} from dead proxy {proxy.host}:{proxy.port}"
                        )
                    await self._controller.db.delete_proxy(proxy.id)
                    count += 1

                self._log(f"[Proxies] Removed {count} dead proxies")

                # Refresh UI
                proxies = await self._controller.db.list_proxies()
                stats = await self._controller.get_stats()
                accounts = await self._controller.search_accounts("")
                accounts_map = {a.id: a for a in accounts}
                self._schedule_ui(lambda: self._update_proxies_table_sync(proxies, accounts_map))
                self._schedule_ui(lambda: self._update_stats_sync(stats))
                self._schedule_ui(lambda: self._update_accounts_table_sync(accounts, {p.id: p for p in proxies}))

            except Exception as e:
                logger.exception("Replace dead proxies error: %s", e)
                self._log(f"[Error] Replace dead: {_se(str(e))}")
            finally:
                self._busy_ops.discard("replace_dead")

        self._run_async(do_replace())


def _startup_health_check() -> None:
    """Verify critical dependencies before launching GUI.

    Checks:
    - Camoufox binary presence
    - Required directories exist (creates if missing)
    """
    from ..paths import ACCOUNTS_DIR, DATA_DIR, LOGS_DIR, PROFILES_DIR

    # Create required directories
    try:
        for d in (ACCOUNTS_DIR, PROFILES_DIR, DATA_DIR, LOGS_DIR):
            d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "TG Web Auth — Error",
                "Cannot create data directories — permission denied."
                + chr(10)
                + chr(10)
                + "Do NOT place TGWebAuth in Program Files or other"
                + chr(10)
                + "protected directories."
                + chr(10)
                + chr(10)
                + "Extract to Desktop or C:\\TGWebAuth\\ instead.",
            )
            root.destroy()
        except Exception as e:
            logger.debug("Failed to show tkinter error dialog: %s", e)
        raise SystemExit(1) from None

    # Check Camoufox binary
    try:
        if getattr(sys, "frozen", False):
            from ..paths import APP_ROOT

            camoufox_path = APP_ROOT / "camoufox" / "camoufox.exe"
            if not camoufox_path.exists():
                raise FileNotFoundError(f"Camoufox not found: {camoufox_path}")
        else:
            from camoufox.pkgman import launch_path

            lp = launch_path()
            if not Path(lp).exists():
                raise FileNotFoundError(f"Camoufox binary not found at {lp}")
    except Exception:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            if getattr(sys, "frozen", False):
                msg = (
                    "Camoufox browser not found in distribution."
                    + chr(10)
                    + chr(10)
                    + "Please re-download TGWebAuth.zip and extract again."
                    + chr(10)
                    + chr(10)
                    + "App will try to continue but migration will fail."
                )
            else:
                msg = (
                    "Camoufox browser not found."
                    + chr(10)
                    + chr(10)
                    + "Install with: python -m camoufox fetch"
                    + chr(10)
                    + chr(10)
                    + "App will try to continue but migration will fail."
                )
            messagebox.showerror("TG Web Auth — Error", msg)
            root.destroy()
        except Exception as e:
            logger.debug("Failed to show tkinter warning dialog: %s", e)
        logger.warning("Camoufox binary not found — migration will fail")


def main():
    """Entry point."""
    # Install global crash handler first
    from ..exception_handler import install_exception_handlers

    install_exception_handlers()

    # Setup logging (RotatingFileHandler + console)
    from ..logger import setup_logging

    setup_logging(level=logging.INFO)

    # Startup health check
    _startup_health_check()

    app = TGWebAuthApp()
    app.run()


if __name__ == "__main__":
    main()
