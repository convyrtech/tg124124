"""Main GUI application."""
import dearpygui.dearpygui as dpg
from pathlib import Path
from typing import Optional, List
import asyncio
import threading
import time
import logging

from .theme import create_hacker_theme, create_status_themes
from .controllers import AppController
from ..database import AccountRecord

logger = logging.getLogger(__name__)


class TGWebAuthApp:
    """Main application window."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path("data")
        self._2fa_password: Optional[str] = None
        self._status_themes: dict = {}
        self._controller = AppController(self.data_dir)
        self._async_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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

        dpg.start_dearpygui()
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
        password = dpg.get_value("2fa_input")
        if password:
            self._2fa_password = password
            self._log("[System] 2FA password set for session")
        dpg.delete_item("2fa_dialog")

    def _on_2fa_skip(self, sender=None, app_data=None) -> None:
        """Skip 2FA password."""
        dpg.delete_item("2fa_dialog")
        self._log("[System] 2FA password skipped - will prompt when needed")

    def _log(self, message: str) -> None:
        """Add message to log."""
        if dpg.does_item_exist("log_output"):
            current = dpg.get_value("log_output")
            dpg.set_value("log_output", current + message + "\n")

    # Import dialog
    def _show_import_dialog(self, sender=None, app_data=None) -> None:
        """Show file dialog to select sessions folder."""
        with dpg.file_dialog(
            directory_selector=True,
            show=True,
            callback=self._on_import_folder_selected,
            width=700,
            height=400
        ):
            pass

    def _on_import_folder_selected(self, sender, app_data) -> None:
        """Handle folder selection for import."""
        if not app_data or "file_path_name" not in app_data:
            return

        folder_path = Path(app_data["file_path_name"])
        self._log(f"[Import] Scanning {folder_path}...")

        async def do_import():
            count = await self._controller.import_sessions(
                folder_path,
                on_progress=lambda done, total: self._log(f"[Import] {done}/{total}")
            )
            self._log(f"[Import] Completed: {count} accounts imported")
            # Refresh UI
            await self._refresh_accounts_table()
            await self._refresh_stats()

        self._run_async(do_import())

    async def _refresh_stats(self) -> None:
        """Refresh header statistics."""
        stats = await self._controller.get_stats()

        dpg.set_value("stat_total", str(stats["total"]))
        dpg.set_value("stat_healthy", str(stats["healthy"]))
        dpg.set_value("stat_migrating", str(stats["migrating"]))
        dpg.set_value("stat_errors", str(stats["errors"]))
        dpg.set_value("stat_proxies", f"{stats['proxies_active']}/{stats['proxies_total']}")

    async def _refresh_accounts_table(self) -> None:
        """Refresh accounts table from database."""
        accounts = await self._controller.search_accounts("")
        await self._update_accounts_table(accounts)

    async def _update_accounts_table(self, accounts: List[AccountRecord]) -> None:
        """Update table with given accounts list."""
        # Clear existing rows
        for child in dpg.get_item_children("accounts_table", 1) or []:
            dpg.delete_item(child)

        # Add rows
        for account in accounts:
            with dpg.table_row(parent="accounts_table"):
                # Checkbox
                dpg.add_checkbox(tag=f"sel_{account.id}")

                # Name (clickable to open profile)
                dpg.add_selectable(
                    label=account.name,
                    callback=self._on_account_click,
                    user_data=account.id
                )

                # Username
                dpg.add_text(account.username or "-")

                # Status with color
                status_text = dpg.add_text(account.status)
                if account.status in self._status_themes:
                    dpg.bind_item_theme(status_text, self._status_themes[account.status])

                # Proxy
                dpg.add_text("-")  # TODO: join with proxy

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

    def _on_search_accounts(self, sender, filter_string) -> None:
        """Handle search input."""
        async def do_search():
            accounts = await self._controller.search_accounts(filter_string)
            await self._update_accounts_table(accounts)

        self._run_async(do_search())

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
        # Get selected checkboxes
        selected_ids = []
        for child in dpg.get_item_children("accounts_table", 1) or []:
            row_children = dpg.get_item_children(child, 1) or []
            if row_children:
                checkbox = row_children[0]
                if dpg.get_value(checkbox):
                    # Extract account_id from checkbox tag
                    tag = dpg.get_item_alias(checkbox)
                    if tag and tag.startswith("sel_"):
                        selected_ids.append(int(tag[4:]))

        if not selected_ids:
            self._log("[Warning] No accounts selected")
            return

        self._log(f"[Migrate] Starting migration of {len(selected_ids)} accounts...")
        # TODO: integrate with telegram_auth

    def _migrate_all(self, sender=None, app_data=None) -> None:
        """Migrate all pending accounts."""
        self._log("[Migrate] Starting batch migration...")
        # TODO: integrate with telegram_auth

    def _show_proxy_import_dialog(self, sender=None, app_data=None) -> None:
        """Show proxy import dialog."""
        if dpg.does_item_exist("proxy_import_dialog"):
            dpg.delete_item("proxy_import_dialog")

        with dpg.window(
            tag="proxy_import_dialog",
            label="Import Proxies",
            modal=True,
            width=500,
            height=400,
            pos=[350, 200]
        ):
            dpg.add_text("Paste proxies (one per line):")
            dpg.add_text("Format: host:port:user:pass or host:port", color=(150, 150, 150))
            dpg.add_spacer(height=10)
            dpg.add_input_text(
                tag="proxy_import_text",
                multiline=True,
                width=-1,
                height=250
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Import", width=100, callback=self._do_import_proxies)
                dpg.add_button(label="Cancel", width=100, callback=lambda: dpg.delete_item("proxy_import_dialog"))

    def _do_import_proxies(self, sender=None, app_data=None) -> None:
        """Execute proxy import."""
        proxy_text = dpg.get_value("proxy_import_text")
        dpg.delete_item("proxy_import_dialog")

        async def do_import():
            count = await self._controller.import_proxies(proxy_text)
            self._log(f"[Import] Imported {count} proxies")
            await self._refresh_stats()

        self._run_async(do_import())

    def _check_all_proxies(self, sender=None, app_data=None) -> None:
        """Check all proxies status."""
        self._log("[Proxies] Checking all proxies...")
        # TODO: implement proxy checking

    def _replace_dead_proxies(self, sender=None, app_data=None) -> None:
        """Replace dead proxies with new ones."""
        self._log("[Proxies] Replacing dead proxies...")
        # TODO: implement proxy replacement


def main():
    """Entry point."""
    logging.basicConfig(level=logging.INFO)
    app = TGWebAuthApp()
    app.run()


if __name__ == "__main__":
    main()
