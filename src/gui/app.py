"""Main GUI application."""
import dearpygui.dearpygui as dpg
from pathlib import Path
from typing import Optional, List, Callable
import asyncio
import threading
import queue
import time
import logging
import traceback

from .theme import create_hacker_theme, create_status_themes
from .controllers import AppController
from ..database import AccountRecord, ProxyRecord

logger = logging.getLogger(__name__)


def _select_folder_tkinter() -> Optional[Path]:
    """Use tkinter for folder selection (more stable than dpg file dialog)."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        # Create hidden root window
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        folder = filedialog.askdirectory(title="Select Sessions Folder")
        root.destroy()

        if folder:
            return Path(folder)
        return None
    except Exception as e:
        logger.error("Tkinter folder dialog error: %s", e)
        return None


def _select_file_tkinter(title: str = "Select File", filetypes: list = None) -> Optional[Path]:
    """Use tkinter for file selection."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        if filetypes is None:
            filetypes = [("Text files", "*.txt"), ("All files", "*.*")]

        filepath = filedialog.askopenfilename(title=title, filetypes=filetypes)
        root.destroy()

        if filepath:
            return Path(filepath)
        return None
    except Exception as e:
        logger.error("Tkinter file dialog error: %s", e)
        return None


class TGWebAuthApp:
    """Main application window."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path("data")
        self._2fa_password: Optional[str] = None
        self._status_themes: dict = {}
        self._controller = AppController(self.data_dir)
        self._async_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Thread-safe queue for UI updates from async code
        self._ui_queue: queue.Queue = queue.Queue()

    def _start_async_loop(self) -> None:
        """Start async event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Initialize controller
        self._loop.run_until_complete(self._controller.initialize())

        # Keep loop running
        self._loop.run_forever()

    def _run_async(self, coro) -> None:
        """Schedule coroutine on async loop."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _schedule_ui(self, func: Callable) -> None:
        """Schedule a function to run on the main UI thread."""
        self._ui_queue.put(func)

    def _process_ui_queue(self) -> None:
        """Process pending UI updates (call from main thread)."""
        while not self._ui_queue.empty():
            try:
                func = self._ui_queue.get_nowait()
                func()
            except Exception as e:
                logger.error("UI queue error: %s", e)
                self._log(f"[Error] UI update failed: {e}")

    def run(self) -> None:
        """Start the application."""
        # Start async thread first
        self._async_thread = threading.Thread(target=self._start_async_loop, daemon=True)
        self._async_thread.start()

        # Give it time to initialize
        time.sleep(0.5)

        dpg.create_context()

        # Apply theme
        theme = create_hacker_theme()
        dpg.bind_theme(theme)
        self._status_themes = create_status_themes()

        # Create main window
        self._create_main_window()

        # Create viewport
        dpg.create_viewport(
            title="TG Web Auth",
            width=1200,
            height=800,
            min_width=800,
            min_height=600
        )

        dpg.setup_dearpygui()
        dpg.show_viewport()

        # Show 2FA dialog on start
        self._show_2fa_dialog()

        # Custom render loop to process UI queue
        while dpg.is_dearpygui_running():
            self._process_ui_queue()
            dpg.render_dearpygui_frame()

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
                tag="account_search",
                hint="Search accounts...",
                width=300,
                callback=self._on_search_accounts
            )
            dpg.add_spacer(width=20)
            dpg.add_button(
                label="Import Sessions",
                callback=self._show_import_dialog
            )
            dpg.add_button(
                label="Migrate Selected",
                callback=self._migrate_selected
            )
            dpg.add_button(
                label="Migrate All",
                callback=self._migrate_all
            )

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
            sortable=True
        ):
            dpg.add_table_column(label="", width_fixed=True, width=30)  # Checkbox
            dpg.add_table_column(label="Name", width=200)
            dpg.add_table_column(label="Username", width=150)
            dpg.add_table_column(label="Status", width=100)
            dpg.add_table_column(label="Proxy", width=180)
            dpg.add_table_column(label="Actions", width=150)

    def _create_proxies_tab(self) -> None:
        """Create proxies management tab."""
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Import Proxies",
                callback=self._show_proxy_import_dialog
            )
            dpg.add_button(
                label="Check All",
                callback=self._check_all_proxies
            )
            dpg.add_button(
                label="Replace Dead",
                callback=self._replace_dead_proxies
            )

        dpg.add_spacer(height=10)

        with dpg.table(
            tag="proxies_table",
            header_row=True,
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            borders_outerV=True,
            row_background=True,
            resizable=True
        ):
            dpg.add_table_column(label="Host:Port", width=180)
            dpg.add_table_column(label="Protocol", width=80)
            dpg.add_table_column(label="Status", width=80)
            dpg.add_table_column(label="Assigned To", width=150)
            dpg.add_table_column(label="Actions", width=100)

    def _create_logs_tab(self) -> None:
        """Create logs tab."""
        dpg.add_input_text(
            tag="log_output",
            multiline=True,
            readonly=True,
            width=-1,
            height=-1,
            default_value="[System] TG Web Auth started\n"
        )

    def _show_2fa_dialog(self) -> None:
        """Show 2FA password input dialog on startup."""
        with dpg.window(
            tag="2fa_dialog",
            label="2FA Password",
            modal=True,
            no_close=True,
            width=400,
            height=180,
            pos=[400, 250]
        ):
            dpg.add_text("Enter 2FA password for batch operations:")
            dpg.add_text("(Leave empty to enter manually each time)", color=(150, 150, 150))
            dpg.add_spacer(height=10)
            dpg.add_input_text(
                tag="2fa_input",
                password=True,
                width=-1,
                on_enter=True,
                callback=self._on_2fa_submit
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="OK", width=100, callback=self._on_2fa_submit)
                dpg.add_button(label="Skip", width=100, callback=self._on_2fa_skip)

    def _on_2fa_submit(self, sender=None, app_data=None) -> None:
        """Handle 2FA password submission."""
        try:
            password = dpg.get_value("2fa_input")
            if password:
                self._2fa_password = password
                self._log("[System] 2FA password set for session")
            dpg.delete_item("2fa_dialog")
        except Exception as e:
            logger.error("2FA submit error: %s\n%s", e, traceback.format_exc())
            self._log(f"[Error] 2FA: {e}")

    def _on_2fa_skip(self, sender=None, app_data=None) -> None:
        """Skip 2FA password."""
        try:
            dpg.delete_item("2fa_dialog")
            self._log("[System] 2FA password skipped - will prompt when needed")
        except Exception as e:
            logger.error("2FA skip error: %s", e)

    def _log(self, message: str) -> None:
        """Add message to log (thread-safe)."""
        def do_log():
            if dpg.does_item_exist("log_output"):
                current = dpg.get_value("log_output")
                dpg.set_value("log_output", current + message + "\n")

        # If called from main thread, do directly; otherwise queue
        try:
            if threading.current_thread() is threading.main_thread():
                do_log()
            else:
                self._schedule_ui(do_log)
        except Exception as e:
            logger.error("Log error: %s", e)

    # Import dialog
    def _show_import_dialog(self, sender=None, app_data=None) -> None:
        """Show file dialog to select sessions folder."""
        try:
            # Use tkinter dialog (more stable than dpg file dialog)
            folder_path = _select_folder_tkinter()
            if folder_path is None:
                self._log("[Import] Cancelled")
                return

            self._log(f"[Import] Scanning {folder_path}...")

            async def do_import():
                try:
                    count = await self._controller.import_sessions(
                        folder_path,
                        on_progress=lambda done, total: self._log(f"[Import] {done}/{total}")
                    )
                    self._log(f"[Import] Completed: {count} accounts imported")

                    # Schedule UI updates on main thread
                    accounts = await self._controller.search_accounts("")
                    stats = await self._controller.get_stats()

                    self._schedule_ui(lambda: self._update_stats_sync(stats))
                    self._schedule_ui(lambda: self._update_accounts_table_sync(accounts))

                except Exception as e:
                    logger.error("Import error: %s\n%s", e, traceback.format_exc())
                    self._log(f"[Error] Import failed: {e}")

            self._run_async(do_import())

        except Exception as e:
            logger.error("Import dialog error: %s\n%s", e, traceback.format_exc())
            self._log(f"[Error] Import: {e}")

    def _update_stats_sync(self, stats: dict) -> None:
        """Update stats on main thread."""
        try:
            dpg.set_value("stat_total", str(stats["total"]))
            dpg.set_value("stat_healthy", str(stats["healthy"]))
            dpg.set_value("stat_migrating", str(stats["migrating"]))
            dpg.set_value("stat_errors", str(stats["errors"]))
            dpg.set_value("stat_proxies", f"{stats['proxies_active']}/{stats['proxies_total']}")
        except Exception as e:
            logger.error("Update stats error: %s", e)

    def _update_accounts_table_sync(self, accounts: List[AccountRecord]) -> None:
        """Update table on main thread."""
        try:
            # Clear existing rows
            for child in dpg.get_item_children("accounts_table", 1) or []:
                dpg.delete_item(child)

            # Add rows
            for account in accounts:
                with dpg.table_row(parent="accounts_table"):
                    # Checkbox
                    dpg.add_checkbox(tag=f"sel_{account.id}")

                    # Name
                    dpg.add_text(account.name)

                    # Username
                    dpg.add_text(account.username or "-")

                    # Status with color
                    status_text = dpg.add_text(account.status)
                    if account.status in self._status_themes:
                        dpg.bind_item_theme(status_text, self._status_themes[account.status])

                    # Proxy
                    dpg.add_text("-")

                    # Actions
                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label="Open",
                            callback=self._open_profile,
                            user_data=account.id,
                            width=60
                        )
                        dpg.add_button(
                            label="Migrate",
                            callback=self._migrate_single,
                            user_data=account.id,
                            width=60
                        )
        except Exception as e:
            logger.error("Update accounts table error: %s\n%s", e, traceback.format_exc())
            self._log(f"[Error] Table update: {e}")

    def _update_proxies_table_sync(self, proxies: list) -> None:
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

                    # Assigned To
                    assigned = str(proxy.assigned_account_id) if proxy.assigned_account_id else "-"
                    dpg.add_text(assigned)

                    # Actions
                    dpg.add_button(
                        label="Delete",
                        callback=self._delete_proxy,
                        user_data=proxy.id,
                        width=60
                    )

            self._log(f"[UI] Loaded {len(proxies)} proxies")
        except Exception as e:
            logger.error("Update proxies table error: %s\n%s", e, traceback.format_exc())
            self._log(f"[Error] Proxies table: {e}")

    def _delete_proxy(self, sender, app_data, user_data) -> None:
        """Delete a proxy."""
        proxy_id = user_data
        self._log(f"[Action] Delete proxy {proxy_id} - not implemented yet")

    def _on_search_accounts(self, sender, filter_string) -> None:
        """Handle search input."""
        try:
            async def do_search():
                try:
                    accounts = await self._controller.search_accounts(filter_string)
                    self._schedule_ui(lambda: self._update_accounts_table_sync(accounts))
                except Exception as e:
                    logger.error("Search error: %s", e)
                    self._log(f"[Error] Search: {e}")

            self._run_async(do_search())
        except Exception as e:
            logger.error("Search accounts error: %s", e)

    def _on_account_click(self, sender, app_data, user_data) -> None:
        """Handle account row click."""
        account_id = user_data
        self._log(f"[UI] Selected account {account_id}")

    def _open_profile(self, sender, app_data, user_data) -> None:
        """Open browser profile for account."""
        account_id = user_data
        self._log(f"[Action] Opening profile for account {account_id}...")
        # TODO: integrate with browser_manager

    def _migrate_single(self, sender, app_data, user_data) -> None:
        """Migrate single account."""
        account_id = user_data
        self._log(f"[Action] Starting migration for account {account_id}...")
        # TODO: integrate with telegram_auth

    def _migrate_selected(self, sender=None, app_data=None) -> None:
        """Migrate all selected accounts."""
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
                            selected_ids.append(int(tag[4:]))

            if not selected_ids:
                self._log("[Warning] No accounts selected")
                return

            self._log(f"[Migrate] Starting migration of {len(selected_ids)} accounts...")
            # TODO: integrate with telegram_auth
        except Exception as e:
            logger.error("Migrate selected error: %s", e)
            self._log(f"[Error] Migrate: {e}")

    def _migrate_all(self, sender=None, app_data=None) -> None:
        """Migrate all pending accounts."""
        self._log("[Migrate] Starting batch migration...")
        # TODO: integrate with telegram_auth

    def _show_proxy_import_dialog(self, sender=None, app_data=None) -> None:
        """Show proxy import - select file directly."""
        try:
            filepath = _select_file_tkinter(
                title="Select Proxies File",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )

            if filepath is None:
                self._log("[Import] Proxy import cancelled")
                return

            self._log(f"[Import] Loading proxies from {filepath.name}...")

            # Read file
            proxy_text = filepath.read_text(encoding='utf-8', errors='ignore')

            async def do_import():
                try:
                    count = await self._controller.import_proxies(proxy_text)
                    self._log(f"[Import] Imported {count} proxies")

                    # Refresh UI
                    stats = await self._controller.get_stats()
                    proxies = await self._controller.db.list_proxies()

                    self._schedule_ui(lambda: self._update_stats_sync(stats))
                    self._schedule_ui(lambda: self._update_proxies_table_sync(proxies))
                except Exception as e:
                    logger.error("Proxy import error: %s\n%s", e, traceback.format_exc())
                    self._log(f"[Error] Proxy import: {e}")

            self._run_async(do_import())

        except Exception as e:
            logger.error("Show proxy import dialog error: %s\n%s", e, traceback.format_exc())
            self._log(f"[Error] Proxy import: {e}")

    def _check_all_proxies(self, sender=None, app_data=None) -> None:
        """Check all proxies status."""
        self._log("[Proxies] Starting proxy check...")

        async def do_check():
            try:
                proxies = await self._controller.db.list_proxies()
                total = len(proxies)
                alive = 0
                dead = 0

                for i, proxy in enumerate(proxies):
                    self._log(f"[Proxies] Checking {i+1}/{total}: {proxy.host}:{proxy.port}")

                    is_alive = await self._controller.check_proxy(proxy)

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
                self._schedule_ui(lambda: self._update_proxies_table_sync(proxies))
                self._schedule_ui(lambda: self._update_stats_sync(stats))

            except Exception as e:
                logger.error("Check proxies error: %s\n%s", e, traceback.format_exc())
                self._log(f"[Error] Proxy check: {e}")

        self._run_async(do_check())

    def _replace_dead_proxies(self, sender=None, app_data=None) -> None:
        """Replace dead proxies with new ones."""
        self._log("[Proxies] Replacing dead proxies...")
        # TODO: implement proxy replacement


def main():
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    app = TGWebAuthApp()
    app.run()


if __name__ == "__main__":
    main()
