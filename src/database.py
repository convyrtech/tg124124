"""
SQLite database for TG Web Auth metadata.
Sessions remain as files for portability.
"""
import sqlite3
import aiosqlite
from pathlib import Path
from typing import Optional, List, Any
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
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        # Enable WAL mode for better concurrency
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # WAL might already be set or locked
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

    async def delete_proxy(self, proxy_id: int) -> None:
        """Delete proxy by ID."""
        await self._connection.execute(
            "DELETE FROM proxies WHERE id = ?",
            (proxy_id,)
        )
        await self._connection.commit()

    async def update_proxy(self, proxy_id: int, **kwargs) -> None:
        """Update proxy fields."""
        if not kwargs:
            return

        # Add last_check timestamp when status changes
        if "status" in kwargs:
            kwargs["last_check"] = "datetime('now')"

        fields = []
        values = []
        for k, v in kwargs.items():
            if v == "datetime('now')":
                fields.append(f"{k} = datetime('now')")
            else:
                fields.append(f"{k} = ?")
                values.append(v)

        values.append(proxy_id)

        await self._connection.execute(
            f"UPDATE proxies SET {', '.join(fields)} WHERE id = ?",
            values
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
