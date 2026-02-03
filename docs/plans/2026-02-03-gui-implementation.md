# TG Web Auth GUI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Создать десктопное приложение с хакерским UI для управления 1000 Telegram аккаунтами — простое для не-технических пользователей.

**Architecture:** Dear PyGui GUI поверх существующего Python-кода. SQLite для метаданных + файлы для сессий. Async event loop для неблокирующих операций. PyInstaller для сборки в .exe.

**Tech Stack:** Dear PyGui, SQLite (aiosqlite), asyncio, PyInstaller

---

## Phase 1: Foundation (Database + Core Structure)

### Task 1: Create SQLite Database Schema

**Files:**
- Create: `src/database.py`
- Create: `tests/test_database.py`

**Step 1: Write the failing test**

```python
# tests/test_database.py
import pytest
import asyncio
from pathlib import Path


class TestDatabase:
    @pytest.fixture
    def db_path(self, tmp_path):
        return tmp_path / "test.db"

    def test_create_tables(self, db_path):
        from src.database import Database

        db = Database(db_path)
        asyncio.run(db.initialize())

        # Check tables exist
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "accounts" in tables
        assert "proxies" in tables
        assert "migrations" in tables
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_database.py::TestDatabase::test_create_tables -v`
Expected: FAIL with "No module named 'src.database'"

**Step 3: Write minimal implementation**

```python
# src/database.py
"""
SQLite database for TG Web Auth metadata.
Sessions remain as files for portability.
"""
import sqlite3
import aiosqlite
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class AccountRecord:
    """Account metadata stored in database."""
    id: int
    name: str
    phone: Optional[str]
    username: Optional[str]
    session_path: str
    proxy_id: Optional[int]
    status: str  # pending, healthy, error, migrating
    last_check: Optional[datetime]
    error_message: Optional[str]
    created_at: datetime


@dataclass
class ProxyRecord:
    """Proxy metadata stored in database."""
    id: int
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    protocol: str  # socks5, http
    status: str  # active, dead, reserved
    assigned_account_id: Optional[int]
    last_check: Optional[datetime]
    created_at: datetime


class Database:
    """SQLite database manager for TG Web Auth."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create database and tables if not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Use sync sqlite3 for initial schema creation
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                username TEXT,
                session_path TEXT NOT NULL UNIQUE,
                proxy_id INTEGER REFERENCES proxies(id),
                status TEXT DEFAULT 'pending',
                last_check TIMESTAMP,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password TEXT,
                protocol TEXT DEFAULT 'socks5',
                status TEXT DEFAULT 'active',
                assigned_account_id INTEGER,
                last_check TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(host, port)
            );

            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES accounts(id),
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                success INTEGER,
                error_message TEXT,
                profile_path TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_proxies_status ON proxies(status);
        """)
        conn.commit()
        conn.close()
        logger.info("Database initialized: %s", self.db_path)

    async def connect(self) -> None:
        """Open async connection."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row

    async def close(self) -> None:
        """Close connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_database.py::TestDatabase::test_create_tables -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat(db): add SQLite database schema for accounts and proxies"
```

---

### Task 2: Add Account CRUD Operations

**Files:**
- Modify: `src/database.py`
- Modify: `tests/test_database.py`

**Step 1: Write the failing test**

```python
# tests/test_database.py - add to TestDatabase class

    @pytest.mark.asyncio
    async def test_add_and_get_account(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add account
            account_id = await db.add_account(
                name="Test Account",
                session_path="/path/to/session.session",
                phone="+1234567890"
            )

            assert account_id > 0

            # Get account
            account = await db.get_account(account_id)

            assert account is not None
            assert account.name == "Test Account"
            assert account.phone == "+1234567890"
            assert account.status == "pending"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_list_accounts_with_filter(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            await db.add_account(name="Account 1", session_path="/a.session", status="healthy")
            await db.add_account(name="Account 2", session_path="/b.session", status="error")
            await db.add_account(name="Account 3", session_path="/c.session", status="healthy")

            all_accounts = await db.list_accounts()
            assert len(all_accounts) == 3

            healthy = await db.list_accounts(status="healthy")
            assert len(healthy) == 2
        finally:
            await db.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_database.py::TestDatabase::test_add_and_get_account -v`
Expected: FAIL with "AttributeError: 'Database' object has no attribute 'add_account'"

**Step 3: Write minimal implementation**

```python
# src/database.py - add methods to Database class

    async def add_account(
        self,
        name: str,
        session_path: str,
        phone: Optional[str] = None,
        username: Optional[str] = None,
        proxy_id: Optional[int] = None,
        status: str = "pending"
    ) -> int:
        """Add new account, return ID."""
        async with self._connection.execute(
            """
            INSERT INTO accounts (name, session_path, phone, username, proxy_id, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, session_path, phone, username, proxy_id, status)
        ) as cursor:
            await self._connection.commit()
            return cursor.lastrowid

    async def get_account(self, account_id: int) -> Optional[AccountRecord]:
        """Get account by ID."""
        async with self._connection.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return AccountRecord(
                    id=row["id"],
                    name=row["name"],
                    phone=row["phone"],
                    username=row["username"],
                    session_path=row["session_path"],
                    proxy_id=row["proxy_id"],
                    status=row["status"],
                    last_check=row["last_check"],
                    error_message=row["error_message"],
                    created_at=row["created_at"]
                )
            return None

    async def list_accounts(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[AccountRecord]:
        """List accounts with optional filters."""
        query = "SELECT * FROM accounts WHERE 1=1"
        params: List[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if search:
            query += " AND (name LIKE ? OR username LIKE ? OR phone LIKE ?)"
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])

        query += " ORDER BY name"

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                AccountRecord(
                    id=row["id"],
                    name=row["name"],
                    phone=row["phone"],
                    username=row["username"],
                    session_path=row["session_path"],
                    proxy_id=row["proxy_id"],
                    status=row["status"],
                    last_check=row["last_check"],
                    error_message=row["error_message"],
                    created_at=row["created_at"]
                )
                for row in rows
            ]

    async def update_account(self, account_id: int, **kwargs) -> None:
        """Update account fields."""
        if not kwargs:
            return

        fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [account_id]

        await self._connection.execute(
            f"UPDATE accounts SET {fields} WHERE id = ?",
            values
        )
        await self._connection.commit()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_database.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat(db): add account CRUD operations"
```

---

### Task 3: Add Proxy CRUD and Auto-Assignment

**Files:**
- Modify: `src/database.py`
- Modify: `tests/test_database.py`

**Step 1: Write the failing test**

```python
# tests/test_database.py - add tests

    @pytest.mark.asyncio
    async def test_add_proxy_and_assign(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add proxy
            proxy_id = await db.add_proxy(
                host="192.168.1.1",
                port=1080,
                username="user",
                password="pass"
            )
            assert proxy_id > 0

            # Add account
            account_id = await db.add_account(
                name="Test",
                session_path="/test.session"
            )

            # Assign proxy to account
            await db.assign_proxy(account_id, proxy_id)

            # Verify
            account = await db.get_account(account_id)
            assert account.proxy_id == proxy_id

            proxy = await db.get_proxy(proxy_id)
            assert proxy.assigned_account_id == account_id
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_get_free_proxy(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add proxies
            p1 = await db.add_proxy(host="1.1.1.1", port=1080)
            p2 = await db.add_proxy(host="2.2.2.2", port=1080)

            # Get free proxy
            free = await db.get_free_proxy()
            assert free is not None
            assert free.id in [p1, p2]

            # Assign it
            acc = await db.add_account(name="A", session_path="/a.session")
            await db.assign_proxy(acc, free.id)

            # Get another free proxy
            free2 = await db.get_free_proxy()
            assert free2 is not None
            assert free2.id != free.id
        finally:
            await db.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_database.py::TestDatabase::test_add_proxy_and_assign -v`
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# src/database.py - add to Database class

    async def add_proxy(
        self,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        protocol: str = "socks5"
    ) -> int:
        """Add new proxy, return ID."""
        async with self._connection.execute(
            """
            INSERT INTO proxies (host, port, username, password, protocol)
            VALUES (?, ?, ?, ?, ?)
            """,
            (host, port, username, password, protocol)
        ) as cursor:
            await self._connection.commit()
            return cursor.lastrowid

    async def get_proxy(self, proxy_id: int) -> Optional[ProxyRecord]:
        """Get proxy by ID."""
        async with self._connection.execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return ProxyRecord(
                    id=row["id"],
                    host=row["host"],
                    port=row["port"],
                    username=row["username"],
                    password=row["password"],
                    protocol=row["protocol"],
                    status=row["status"],
                    assigned_account_id=row["assigned_account_id"],
                    last_check=row["last_check"],
                    created_at=row["created_at"]
                )
            return None

    async def get_free_proxy(self) -> Optional[ProxyRecord]:
        """Get unassigned active proxy."""
        async with self._connection.execute(
            """
            SELECT * FROM proxies
            WHERE status = 'active' AND assigned_account_id IS NULL
            ORDER BY last_check ASC NULLS FIRST
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return ProxyRecord(
                    id=row["id"],
                    host=row["host"],
                    port=row["port"],
                    username=row["username"],
                    password=row["password"],
                    protocol=row["protocol"],
                    status=row["status"],
                    assigned_account_id=row["assigned_account_id"],
                    last_check=row["last_check"],
                    created_at=row["created_at"]
                )
            return None

    async def assign_proxy(self, account_id: int, proxy_id: int) -> None:
        """Assign proxy to account (1:1 binding)."""
        await self._connection.execute(
            "UPDATE accounts SET proxy_id = ? WHERE id = ?",
            (proxy_id, account_id)
        )
        await self._connection.execute(
            "UPDATE proxies SET assigned_account_id = ? WHERE id = ?",
            (account_id, proxy_id)
        )
        await self._connection.commit()

    async def list_proxies(
        self,
        status: Optional[str] = None,
        unassigned_only: bool = False
    ) -> List[ProxyRecord]:
        """List proxies with filters."""
        query = "SELECT * FROM proxies WHERE 1=1"
        params: List[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if unassigned_only:
            query += " AND assigned_account_id IS NULL"

        query += " ORDER BY host, port"

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                ProxyRecord(
                    id=row["id"],
                    host=row["host"],
                    port=row["port"],
                    username=row["username"],
                    password=row["password"],
                    protocol=row["protocol"],
                    status=row["status"],
                    assigned_account_id=row["assigned_account_id"],
                    last_check=row["last_check"],
                    created_at=row["created_at"]
                )
                for row in rows
            ]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_database.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat(db): add proxy CRUD and auto-assignment"
```

---

## Phase 2: GUI Foundation

### Task 4: Create Basic Dear PyGui Window with Hacker Theme

**Files:**
- Create: `src/gui/__init__.py`
- Create: `src/gui/app.py`
- Create: `src/gui/theme.py`

**Step 1: Create hacker theme**

```python
# src/gui/theme.py
"""Hacker-style dark theme for Dear PyGui."""
import dearpygui.dearpygui as dpg


def create_hacker_theme() -> int:
    """Create dark green hacker theme. Returns theme ID."""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            # Background colors - dark
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (22, 27, 34))

            # Text - green
            dpg.add_theme_color(dpg.mvThemeCol_Text, (88, 166, 92))
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (68, 85, 68))

            # Borders
            dpg.add_theme_color(dpg.mvThemeCol_Border, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))

            # Frame (input fields, etc)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (33, 38, 45))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (44, 49, 56))

            # Title bar
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (22, 27, 34))

            # Buttons
            dpg.add_theme_color(dpg.mvThemeCol_Button, (35, 134, 54))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (46, 160, 67))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (29, 111, 45))

            # Headers (tables, etc)
            dpg.add_theme_color(dpg.mvThemeCol_Header, (35, 134, 54, 80))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (46, 160, 67, 100))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (29, 111, 45, 120))

            # Selection
            dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, (35, 134, 54, 100))

            # Scrollbar
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (58, 64, 71))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, (68, 74, 81))

            # Tab
            dpg.add_theme_color(dpg.mvThemeCol_Tab, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, (35, 134, 54))
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, (35, 134, 54))

            # Table
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (22, 27, 34))
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (48, 54, 61))
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (33, 38, 45))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (13, 17, 23))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (18, 22, 28))

            # Styles
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 4)

    return theme


def create_status_themes() -> dict:
    """Create themes for different status indicators."""
    themes = {}

    # Healthy - bright green
    with dpg.theme() as healthy:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (46, 204, 64))
    themes["healthy"] = healthy

    # Error - red
    with dpg.theme() as error:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 82, 82))
    themes["error"] = error

    # Pending - yellow
    with dpg.theme() as pending:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 193, 7))
    themes["pending"] = pending

    # Migrating - blue
    with dpg.theme() as migrating:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (66, 165, 245))
    themes["migrating"] = migrating

    return themes
```

**Step 2: Create main app**

```python
# src/gui/__init__.py
"""TG Web Auth GUI package."""
from .app import TGWebAuthApp

__all__ = ["TGWebAuthApp"]
```

```python
# src/gui/app.py
"""Main GUI application."""
import dearpygui.dearpygui as dpg
from pathlib import Path
from typing import Optional
import asyncio
import logging

from .theme import create_hacker_theme, create_status_themes

logger = logging.getLogger(__name__)


class TGWebAuthApp:
    """Main application window."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path("data")
        self._2fa_password: Optional[str] = None
        self._status_themes: dict = {}

    def run(self) -> None:
        """Start the application."""
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

                dpg.add_text("✓", color=(46, 204, 64))
                dpg.add_text("0", tag="stat_healthy")
                dpg.add_spacer(width=10)

                dpg.add_text("⟳", color=(66, 165, 245))
                dpg.add_text("0", tag="stat_migrating")
                dpg.add_spacer(width=10)

                dpg.add_text("✗", color=(255, 82, 82))
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

    def _on_2fa_submit(self, sender, app_data) -> None:
        """Handle 2FA password submission."""
        password = dpg.get_value("2fa_input")
        if password:
            self._2fa_password = password
            self._log("[System] 2FA password set for session")
        dpg.delete_item("2fa_dialog")

    def _on_2fa_skip(self) -> None:
        """Skip 2FA password."""
        dpg.delete_item("2fa_dialog")
        self._log("[System] 2FA password skipped - will prompt when needed")

    def _log(self, message: str) -> None:
        """Add message to log."""
        current = dpg.get_value("log_output")
        dpg.set_value("log_output", current + message + "\n")

    # Placeholder callbacks
    def _on_search_accounts(self, sender, filter_string) -> None:
        pass

    def _show_import_dialog(self) -> None:
        pass

    def _migrate_selected(self) -> None:
        pass

    def _migrate_all(self) -> None:
        pass

    def _show_proxy_import_dialog(self) -> None:
        pass

    def _check_all_proxies(self) -> None:
        pass

    def _replace_dead_proxies(self) -> None:
        pass


def main():
    """Entry point."""
    app = TGWebAuthApp()
    app.run()


if __name__ == "__main__":
    main()
```

**Step 3: Test manually**

Run: `python -m src.gui.app`
Expected: Window opens with hacker-style dark green theme, 2FA dialog appears

**Step 4: Commit**

```bash
git add src/gui/
git commit -m "feat(gui): add Dear PyGui foundation with hacker theme"
```

---

### Task 5: Integrate Database with GUI

**Files:**
- Modify: `src/gui/app.py`
- Create: `src/gui/controllers.py`

**Step 1: Create controller layer**

```python
# src/gui/controllers.py
"""Business logic controllers for GUI."""
import asyncio
from pathlib import Path
from typing import Optional, List, Callable
import logging

from ..database import Database, AccountRecord, ProxyRecord

logger = logging.getLogger(__name__)


class AppController:
    """Main application controller."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "tgwebauth.db"
        self.sessions_dir = data_dir / "sessions"
        self.db: Optional[Database] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def initialize(self) -> None:
        """Initialize database and directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.db_path)
        await self.db.initialize()
        await self.db.connect()

        logger.info("App initialized: %s", self.data_dir)

    async def shutdown(self) -> None:
        """Cleanup on shutdown."""
        if self.db:
            await self.db.close()

    async def get_stats(self) -> dict:
        """Get account/proxy statistics."""
        accounts = await self.db.list_accounts()
        proxies = await self.db.list_proxies()

        healthy = sum(1 for a in accounts if a.status == "healthy")
        migrating = sum(1 for a in accounts if a.status == "migrating")
        errors = sum(1 for a in accounts if a.status == "error")
        active_proxies = sum(1 for p in proxies if p.status == "active")

        return {
            "total": len(accounts),
            "healthy": healthy,
            "migrating": migrating,
            "errors": errors,
            "proxies_active": active_proxies,
            "proxies_total": len(proxies)
        }

    async def search_accounts(self, query: str) -> List[AccountRecord]:
        """Search accounts by name/username/phone."""
        return await self.db.list_accounts(search=query if query else None)

    async def import_sessions(
        self,
        source_dir: Path,
        on_progress: Optional[Callable[[int, int], None]] = None
    ) -> int:
        """Import session files from directory."""
        imported = 0
        session_files = list(source_dir.glob("**/*.session"))
        total = len(session_files)

        for i, session_path in enumerate(session_files):
            try:
                # Find associated files
                account_dir = session_path.parent
                name = account_dir.name

                # Copy to sessions directory
                dest_dir = self.sessions_dir / name
                dest_dir.mkdir(exist_ok=True)

                dest_session = dest_dir / "session.session"
                dest_session.write_bytes(session_path.read_bytes())

                # Copy api.json if exists
                api_json = account_dir / "api.json"
                if api_json.exists():
                    (dest_dir / "api.json").write_bytes(api_json.read_bytes())

                # Add to database
                await self.db.add_account(
                    name=name,
                    session_path=str(dest_session)
                )

                imported += 1

                if on_progress:
                    on_progress(i + 1, total)

            except Exception as e:
                logger.error("Failed to import %s: %s", session_path, e)

        return imported

    async def import_proxies(self, proxy_list: str) -> int:
        """Import proxies from text (one per line, format: host:port:user:pass)."""
        imported = 0

        for line in proxy_list.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                parts = line.split(":")
                if len(parts) >= 2:
                    host = parts[0]
                    port = int(parts[1])
                    username = parts[2] if len(parts) > 2 else None
                    password = parts[3] if len(parts) > 3 else None

                    await self.db.add_proxy(
                        host=host,
                        port=port,
                        username=username,
                        password=password
                    )
                    imported += 1
            except Exception as e:
                logger.warning("Failed to parse proxy line: %s - %s", line, e)

        return imported
```

**Step 2: Update app.py to use controller**

```python
# src/gui/app.py - update __init__ and add controller integration
# Add at top:
import threading
from .controllers import AppController

# Update __init__:
def __init__(self, data_dir: Optional[Path] = None):
    self.data_dir = data_dir or Path("data")
    self._2fa_password: Optional[str] = None
    self._status_themes: dict = {}
    self._controller = AppController(self.data_dir)
    self._async_thread: Optional[threading.Thread] = None
    self._loop: Optional[asyncio.AbstractEventLoop] = None

# Add async runner:
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

# Update run():
def run(self) -> None:
    # Start async thread first
    self._async_thread = threading.Thread(target=self._start_async_loop, daemon=True)
    self._async_thread.start()

    # Give it time to initialize
    import time
    time.sleep(0.5)

    # Then start GUI...
    dpg.create_context()
    # ... rest of existing code
```

**Step 3: Test manually**

Run: `python -m src.gui.app`
Expected: Window opens, async loop runs in background, no errors

**Step 4: Commit**

```bash
git add src/gui/
git commit -m "feat(gui): integrate database controller with async loop"
```

---

## Phase 3: Import & Display

### Task 6: Implement Session Import Dialog

**Files:**
- Modify: `src/gui/app.py`

**Step 1: Add file dialog and import logic**

```python
# src/gui/app.py - update _show_import_dialog

def _show_import_dialog(self) -> None:
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
        dpg.split_frame()  # Ensure we're on main thread
        await self._refresh_accounts_table()
        await self._refresh_stats()

    self._run_async(do_import())
```

**Step 2: Add table refresh methods**

```python
# src/gui/app.py - add refresh methods

async def _refresh_stats(self) -> None:
    """Refresh header statistics."""
    stats = await self._controller.get_stats()

    # Use dpg.split_frame to ensure UI updates from async
    dpg.set_value("stat_total", str(stats["total"]))
    dpg.set_value("stat_healthy", str(stats["healthy"]))
    dpg.set_value("stat_migrating", str(stats["migrating"]))
    dpg.set_value("stat_errors", str(stats["errors"]))
    dpg.set_value("stat_proxies", f"{stats['proxies_active']}/{stats['proxies_total']}")

async def _refresh_accounts_table(self) -> None:
    """Refresh accounts table from database."""
    accounts = await self._controller.search_accounts("")

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
            dpg.add_text(account.username or "—")

            # Status with color
            status_text = dpg.add_text(account.status)
            if account.status in self._status_themes:
                dpg.bind_item_theme(status_text, self._status_themes[account.status])

            # Proxy
            dpg.add_text("—")  # TODO: join with proxy

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
```

**Step 3: Test manually**

Run: `python -m src.gui.app`
Then: Click "Import Sessions" → Select accounts folder
Expected: Accounts appear in table with status colors

**Step 4: Commit**

```bash
git add src/gui/app.py
git commit -m "feat(gui): add session import and accounts table display"
```

---

### Task 7: Implement Search/Filter

**Files:**
- Modify: `src/gui/app.py`

**Step 1: Add search callback**

```python
# src/gui/app.py - update _on_search_accounts

def _on_search_accounts(self, sender, filter_string) -> None:
    """Handle search input."""
    async def do_search():
        accounts = await self._controller.search_accounts(filter_string)
        await self._update_accounts_table(accounts)

    self._run_async(do_search())

async def _update_accounts_table(self, accounts: list) -> None:
    """Update table with given accounts list."""
    # Clear existing rows
    for child in dpg.get_item_children("accounts_table", 1) or []:
        dpg.delete_item(child)

    # Add rows (same as _refresh_accounts_table but with provided list)
    for account in accounts:
        with dpg.table_row(parent="accounts_table"):
            dpg.add_checkbox(tag=f"sel_{account.id}")
            dpg.add_selectable(
                label=account.name,
                callback=self._on_account_click,
                user_data=account.id
            )
            dpg.add_text(account.username or "—")
            status_text = dpg.add_text(account.status)
            if account.status in self._status_themes:
                dpg.bind_item_theme(status_text, self._status_themes[account.status])
            dpg.add_text("—")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Open", callback=self._open_profile, user_data=account.id, width=60)
                dpg.add_button(label="Migrate", callback=self._migrate_single, user_data=account.id, width=60)
```

**Step 2: Commit**

```bash
git add src/gui/app.py
git commit -m "feat(gui): add account search/filter"
```

---

## Phase 4: Migration Integration

### Task 8: Connect GUI to Existing Migration Code

**Files:**
- Modify: `src/gui/controllers.py`
- Modify: `src/gui/app.py`

**Step 1: Add migration methods to controller**

```python
# src/gui/controllers.py - add to AppController

async def migrate_account(
    self,
    account_id: int,
    password: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None
) -> bool:
    """Migrate single account."""
    from ..telegram_auth import TelegramAuth, AccountConfig
    from ..browser_manager import BrowserManager

    account = await self.db.get_account(account_id)
    if not account:
        return False

    # Update status
    await self.db.update_account(account_id, status="migrating")

    try:
        # Load account config
        session_dir = Path(account.session_path).parent
        config = AccountConfig.load(session_dir)

        # Get proxy if assigned
        if account.proxy_id:
            proxy = await self.db.get_proxy(account.proxy_id)
            if proxy:
                config.proxy = f"{proxy.protocol}:{proxy.host}:{proxy.port}"
                if proxy.username:
                    config.proxy += f":{proxy.username}:{proxy.password or ''}"

        # Run migration
        profiles_dir = self.data_dir / "profiles"
        browser_manager = BrowserManager(profiles_dir=profiles_dir)
        auth = TelegramAuth(browser_manager)

        if on_log:
            on_log(f"Connecting to {account.name}...")

        result = await auth.authorize(
            config,
            password=password,
            headless=False
        )

        if result.success:
            await self.db.update_account(
                account_id,
                status="healthy",
                username=result.user_info.get("username") if result.user_info else None,
                error_message=None
            )
            if on_log:
                on_log(f"✓ {account.name} migrated successfully")
            return True
        else:
            await self.db.update_account(
                account_id,
                status="error",
                error_message=result.error
            )
            if on_log:
                on_log(f"✗ {account.name} failed: {result.error}")
            return False

    except Exception as e:
        await self.db.update_account(
            account_id,
            status="error",
            error_message=str(e)
        )
        if on_log:
            on_log(f"✗ {account.name} error: {e}")
        return False
```

**Step 2: Update GUI migration callbacks**

```python
# src/gui/app.py - update migration methods

def _migrate_single(self, sender, app_data, user_data) -> None:
    """Migrate single account."""
    account_id = user_data

    async def do_migrate():
        success = await self._controller.migrate_account(
            account_id,
            password=self._2fa_password,
            on_log=self._log
        )
        await self._refresh_accounts_table()
        await self._refresh_stats()

    self._run_async(do_migrate())

def _migrate_selected(self) -> None:
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

    async def do_migrate_batch():
        for account_id in selected_ids:
            await self._controller.migrate_account(
                account_id,
                password=self._2fa_password,
                on_log=self._log
            )
            await self._refresh_stats()
        await self._refresh_accounts_table()

    self._run_async(do_migrate_batch())

def _migrate_all(self) -> None:
    """Migrate all pending accounts."""
    async def do_migrate_all():
        accounts = await self._controller.db.list_accounts(status="pending")
        self._log(f"[Migrate] Starting migration of {len(accounts)} accounts...")

        for account in accounts:
            await self._controller.migrate_account(
                account.id,
                password=self._2fa_password,
                on_log=self._log
            )
            await self._refresh_stats()

        await self._refresh_accounts_table()
        self._log("[Migrate] Batch migration completed")

    self._run_async(do_migrate_all())
```

**Step 3: Test manually**

Run: `python -m src.gui.app`
Then: Import accounts → Click "Migrate" on one account
Expected: Migration runs, status updates, logs appear

**Step 4: Commit**

```bash
git add src/gui/
git commit -m "feat(gui): integrate migration with existing telegram_auth code"
```

---

## Phase 5: PyInstaller Build

### Task 9: Create PyInstaller Spec and Build Script

**Files:**
- Create: `tgwebauth.spec`
- Create: `build.py`
- Modify: `requirements.txt`

**Step 1: Add build dependencies**

```txt
# requirements.txt - add
pyinstaller>=6.0
```

**Step 2: Create spec file**

```python
# tgwebauth.spec
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['src/gui/app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('decode_qr.js', '.'),
    ],
    hiddenimports=[
        'telethon',
        'aiosqlite',
        'pproxy',
        'cv2',
        'PIL',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TGWebAuth',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # TODO: Add icon
)
```

**Step 3: Create build script**

```python
# build.py
"""Build TG Web Auth executable."""
import subprocess
import sys
from pathlib import Path


def main():
    print("Building TG Web Auth...")

    # Ensure dependencies
    subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    # Build
    result = subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "tgwebauth.spec"
    ], check=True)

    dist_path = Path("dist/TGWebAuth.exe")
    if dist_path.exists():
        size_mb = dist_path.stat().st_size / (1024 * 1024)
        print(f"\n✓ Build successful: {dist_path} ({size_mb:.1f} MB)")
    else:
        print("\n✗ Build failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

**Step 4: Test build**

Run: `python build.py`
Expected: `dist/TGWebAuth.exe` created (~50-80 MB)

**Step 5: Commit**

```bash
git add tgwebauth.spec build.py requirements.txt
git commit -m "feat(build): add PyInstaller configuration"
```

---

## Summary

| Phase | Tasks | Description |
|-------|-------|-------------|
| 1 | 1-3 | Database foundation (SQLite, accounts, proxies) |
| 2 | 4-5 | GUI foundation (Dear PyGui, theme, async loop) |
| 3 | 6-7 | Import & display (file dialog, table, search) |
| 4 | 8 | Migration integration |
| 5 | 9 | PyInstaller build |

**Total: 9 tasks, ~2-3 hours estimated**

**Dependencies:**
- `pip install dearpygui aiosqlite pyinstaller`
- Existing code: `telegram_auth.py`, `browser_manager.py`

**Testing:**
- Each task has manual test step
- Run `pytest` after each commit to ensure no regressions
