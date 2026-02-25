"""
SQLite database for TG Web Auth metadata.
Sessions remain as files for portability.
"""

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Sentinel for SQL datetime('now') — avoids magic string comparison
_SQL_NOW = object()


@dataclass
class AccountRecord:
    """Account metadata stored in database."""

    id: int
    name: str
    phone: str | None
    username: str | None
    session_path: str
    proxy_id: int | None
    status: str  # pending, healthy, error, migrating
    last_check: datetime | None
    error_message: str | None
    created_at: datetime
    fragment_status: str | None = None
    web_last_verified: str | None = None
    auth_ttl_days: int | None = None  # None, "authorized"


@dataclass
class ProxyRecord:
    """Proxy metadata stored in database."""

    id: int
    host: str
    port: int
    username: str | None
    password: str | None
    protocol: str  # socks5, http
    status: str  # active, dead, reserved
    assigned_account_id: int | None
    last_check: datetime | None
    created_at: datetime


@dataclass
class MigrationRecord:
    """Migration history record."""

    id: int
    account_id: int
    started_at: datetime
    completed_at: datetime | None
    success: bool | None
    error_message: str | None
    profile_path: str | None


class Database:
    """SQLite database manager for TG Web Auth."""

    # Whitelist of allowed fields for update_account to prevent SQL injection
    ALLOWED_ACCOUNT_FIELDS = {
        "name",
        "phone",
        "username",
        "session_path",
        "proxy_id",
        "status",
        "last_check",
        "error_message",
        "fragment_status",
        "web_last_verified",
        "auth_ttl_days",
    }

    # Whitelist for update_proxy
    ALLOWED_PROXY_FIELDS = {
        "host",
        "port",
        "username",
        "password",
        "protocol",
        "status",
        "assigned_account_id",
        "last_check",
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None
        # BUG-2 FIX: Lock to serialize multi-step write operations
        # across concurrent workers sharing a single aiosqlite connection.
        self._db_lock: asyncio.Lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create database and tables if not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _sync_init(db_path: Path) -> None:
            # Use sync sqlite3 for initial schema creation
            with sqlite3.connect(db_path, timeout=30.0) as conn:
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

                    CREATE TABLE IF NOT EXISTS batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id TEXT UNIQUE NOT NULL,
                        total_count INTEGER DEFAULT 0,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        finished_at TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS operation_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER REFERENCES accounts(id),
                        operation TEXT NOT NULL,
                        success INTEGER,
                        error_message TEXT,
                        details TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Safe ALTER TABLE migrations (ignore if column already exists)
                alter_statements = [
                    "ALTER TABLE accounts ADD COLUMN fragment_status TEXT DEFAULT NULL",
                    "ALTER TABLE accounts ADD COLUMN web_last_verified TIMESTAMP DEFAULT NULL",
                    "ALTER TABLE accounts ADD COLUMN auth_ttl_days INTEGER DEFAULT NULL",
                    "ALTER TABLE migrations ADD COLUMN batch_id INTEGER REFERENCES batches(id)",
                ]
                for stmt in alter_statements:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # Column already exists

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_init, self.db_path)
        logger.info("Database initialized: %s", self.db_path)

    async def connect(self) -> None:
        """Open async connection."""
        try:
            self._connection = await aiosqlite.connect(self.db_path)
            self._connection.row_factory = aiosqlite.Row
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA busy_timeout=30000")
            await self._connection.execute("PRAGMA foreign_keys=ON")
        except Exception as e:
            logger.error("Failed to connect to database %s: %s", self.db_path, e)
            self._connection = None
            raise RuntimeError(
                f"Cannot open database: {e}. "
                "Check disk space, file permissions, and that the path contains only ASCII characters."
            ) from e

    async def close(self) -> None:
        """Close connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _commit_with_retry(self, max_retries: int = 3) -> None:
        """Commit with exponential backoff retry on SQLITE_BUSY.

        With 5+ parallel workers, lock contention causes OperationalError.
        busy_timeout=30s handles most cases, but under heavy I/O (disk flush),
        a retry loop provides extra safety.

        If all retries fail, rollback to prevent uncommitted data from leaking
        into the next successful commit.

        Args:
            max_retries: Number of retry attempts (default 3).
        """
        for attempt in range(max_retries):
            try:
                await self._connection.commit()
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    wait = 0.1 * (2**attempt)  # 0.1s, 0.2s, 0.4s
                    logger.warning(
                        "SQLite busy (attempt %d/%d), retrying in %.1fs",
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                # Exhausted retries or non-busy error: rollback to prevent
                # uncommitted writes from leaking into the next commit
                try:
                    await self._connection.rollback()
                except Exception as rollback_err:
                    logger.warning("Rollback after commit failure also failed: %s", rollback_err)
                raise

    async def add_account(
        self,
        name: str,
        session_path: str,
        phone: str | None = None,
        username: str | None = None,
        proxy_id: int | None = None,
        status: str = "pending",
    ) -> tuple[int, bool]:
        """Add new account, return (ID, created).

        Returns:
            Tuple of (account_id, was_created). If account with this name
            already exists, returns (existing_id, False).
        """
        async with self._db_lock:
            # Check for existing account to prevent duplicates
            async with self._connection.execute("SELECT id FROM accounts WHERE name = ? LIMIT 1", (name,)) as cursor:
                existing = await cursor.fetchone()
                if existing:
                    return existing[0], False
            async with self._connection.execute(
                """
                INSERT INTO accounts (name, session_path, phone, username, proxy_id, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, session_path, phone, username, proxy_id, status),
            ) as cursor:
                await self._commit_with_retry()
                return cursor.lastrowid, True

    async def account_exists(self, name: str) -> bool:
        """Check if account with exact name exists. O(1) via index."""
        async with self._connection.execute("SELECT 1 FROM accounts WHERE name = ? LIMIT 1", (name,)) as cursor:
            return await cursor.fetchone() is not None

    async def remove_duplicate_accounts(self) -> int:
        """Remove duplicate accounts keeping the one with most data (lowest ID with proxy/status).

        Returns number of removed duplicates.
        """
        async with self._db_lock:
            # Find duplicates: keep the "richest" row per name group
            # Priority: has proxy_id > non-pending status > lowest id
            async with self._connection.execute("""
                SELECT id FROM accounts
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, name,
                            ROW_NUMBER() OVER (
                                PARTITION BY name
                                ORDER BY
                                    CASE WHEN proxy_id IS NOT NULL THEN 0 ELSE 1 END,
                                    CASE WHEN status != 'pending' THEN 0 ELSE 1 END,
                                    CASE WHEN fragment_status IS NOT NULL THEN 0 ELSE 1 END,
                                    id ASC
                            ) as rn
                        FROM accounts
                    ) WHERE rn = 1
                )
                AND name IN (SELECT name FROM accounts GROUP BY name HAVING COUNT(*) > 1)
            """) as cursor:
                dup_rows = await cursor.fetchall()

            if not dup_rows:
                return 0

            dup_ids = [r["id"] for r in dup_rows]
            placeholders = ",".join("?" * len(dup_ids))

            # Clean up related records before deleting accounts (foreign keys may not CASCADE)
            await self._connection.execute(
                f"DELETE FROM migrations WHERE account_id IN ({placeholders})", dup_ids
            )
            await self._connection.execute(
                f"DELETE FROM operation_log WHERE account_id IN ({placeholders})", dup_ids
            )
            # Delete duplicates (keeping first occurrence)
            await self._connection.execute(f"DELETE FROM accounts WHERE id IN ({placeholders})", dup_ids)
            await self._commit_with_retry()
            logger.info("Removed %d duplicate accounts", len(dup_ids))
            return len(dup_ids)

    async def get_account(self, account_id: int) -> AccountRecord | None:
        """Get account by ID."""
        async with self._connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)) as cursor:
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
                    created_at=row["created_at"],
                    fragment_status=row["fragment_status"],
                    web_last_verified=row["web_last_verified"],
                    auth_ttl_days=row["auth_ttl_days"],
                )
            return None

    async def list_accounts(self, status: str | None = None, search: str | None = None) -> list[AccountRecord]:
        """List accounts with optional filters."""
        query = "SELECT * FROM accounts WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if search:
            query += " AND (name LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR phone LIKE ? ESCAPE '\\')"
            # Escape LIKE wildcards in user input
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
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
                    created_at=row["created_at"],
                    fragment_status=row["fragment_status"],
                    web_last_verified=row["web_last_verified"],
                    auth_ttl_days=row["auth_ttl_days"],
                )
                for row in rows
            ]

    async def get_counts(self) -> dict:
        """FIX-7.2: Get account/proxy counts with SQL aggregation (no full row load).

        Returns:
            Dict with account status counts and proxy counts.
        """
        result = {
            "total": 0,
            "healthy": 0,
            "migrating": 0,
            "errors": 0,
            "fragment_authorized": 0,
            "proxies_active": 0,
            "proxies_total": 0,
        }
        # Account status counts
        async with self._connection.execute("SELECT status, COUNT(*) as cnt FROM accounts GROUP BY status") as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                result["total"] += row["cnt"]
                if row["status"] == "healthy":
                    result["healthy"] = row["cnt"]
                elif row["status"] == "migrating":
                    result["migrating"] = row["cnt"]
                elif row["status"] == "error":
                    result["errors"] = row["cnt"]

        # Fragment status
        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM accounts WHERE fragment_status = 'authorized'"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                result["fragment_authorized"] = row["cnt"]

        # Proxy counts
        async with self._connection.execute("SELECT status, COUNT(*) as cnt FROM proxies GROUP BY status") as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                result["proxies_total"] += row["cnt"]
                if row["status"] == "active":
                    result["proxies_active"] = row["cnt"]

        return result

    async def update_account(self, account_id: int, **kwargs) -> None:
        """Update account fields with SQL injection protection."""
        if not kwargs:
            return

        # Validate field names against whitelist
        invalid_fields = set(kwargs.keys()) - self.ALLOWED_ACCOUNT_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid account fields: {invalid_fields}")

        fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = [*list(kwargs.values()), account_id]

        async with self._db_lock:
            await self._connection.execute(f"UPDATE accounts SET {fields} WHERE id = ?", values)
            await self._commit_with_retry()

    async def add_proxy(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        protocol: str = "socks5",
    ) -> int:
        """Add new proxy, return ID."""
        async with self._db_lock:
            async with self._connection.execute(
                """
                INSERT INTO proxies (host, port, username, password, protocol)
                VALUES (?, ?, ?, ?, ?)
                """,
                (host, port, username, password, protocol),
            ) as cursor:
                await self._commit_with_retry()
                return cursor.lastrowid

    async def get_proxy(self, proxy_id: int) -> ProxyRecord | None:
        """Get proxy by ID."""
        async with self._connection.execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)) as cursor:
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
                    created_at=row["created_at"],
                )
            return None

    async def find_proxy_by_host_port(self, host: str, port: int) -> int | None:
        """Find proxy ID by host and port. O(1) via UNIQUE index.

        Args:
            host: Proxy hostname.
            port: Proxy port.

        Returns:
            Proxy ID if found, None otherwise.
        """
        async with self._connection.execute(
            "SELECT id FROM proxies WHERE host = ? AND port = ?", (host, port)
        ) as cursor:
            row = await cursor.fetchone()
            return row["id"] if row else None

    async def get_free_proxy(self) -> ProxyRecord | None:
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
                    created_at=row["created_at"],
                )
            return None

    async def assign_proxy(self, account_id: int, proxy_id: int, *, allow_shared: bool = False) -> None:
        """Assign proxy to account.

        By default enforces 1:1 binding (dedicated proxies). With allow_shared=True,
        multiple accounts can share the same proxy (for rotating/residential proxies).

        Uses atomic check-and-update to prevent TOCTOU race where
        two accounts could get the same proxy assigned.

        BUG-2 FIX: Wrapped in _db_lock to prevent interleaving.

        Args:
            account_id: Account to assign proxy to.
            proxy_id: Proxy to assign.
            allow_shared: If True, skip exclusivity check (rotating proxies).

        Raises:
            ValueError: If proxy is already assigned to a different account
                        (only when allow_shared=False).
        """
        async with self._db_lock:
            if allow_shared:
                # Just verify proxy exists
                async with self._connection.execute(
                    "SELECT id FROM proxies WHERE id = ?", (proxy_id,)
                ) as check:
                    if await check.fetchone() is None:
                        raise ValueError(f"Proxy {proxy_id} not found")
            else:
                # FIX-P1: Atomic check-and-update — only assigns if unassigned
                # or already assigned to this same account
                async with self._connection.execute(
                    """UPDATE proxies
                       SET assigned_account_id = ?
                       WHERE id = ?
                         AND (assigned_account_id IS NULL OR assigned_account_id = ?)""",
                    (account_id, proxy_id, account_id),
                ) as cursor:
                    if cursor.rowcount == 0:
                        # Either proxy doesn't exist or is assigned to another account
                        async with self._connection.execute(
                            "SELECT assigned_account_id FROM proxies WHERE id = ?", (proxy_id,)
                        ) as check:
                            row = await check.fetchone()
                            if row is None:
                                raise ValueError(f"Proxy {proxy_id} not found")
                            raise ValueError(f"Proxy {proxy_id} already assigned to account {row[0]}")

            await self._connection.execute("UPDATE accounts SET proxy_id = ? WHERE id = ?", (proxy_id, account_id))
            await self._commit_with_retry()

    async def delete_proxy(self, proxy_id: int) -> None:
        """Delete proxy by ID.

        Also clears proxy_id on any account that references this proxy,
        preventing dangling foreign key references and accidental
        proxy-less migrations (which would leak the user's real IP).
        """
        async with self._db_lock:
            await self._connection.execute(
                "UPDATE accounts SET proxy_id = NULL WHERE proxy_id = ?", (proxy_id,)
            )
            await self._connection.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
            await self._commit_with_retry()

    async def update_proxy(self, proxy_id: int, **kwargs) -> None:
        """Update proxy fields with SQL injection protection."""
        if not kwargs:
            return

        # Validate field names against whitelist
        invalid_fields = set(kwargs.keys()) - self.ALLOWED_PROXY_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid proxy fields: {invalid_fields}")

        # Add last_check timestamp when status changes
        if "status" in kwargs:
            kwargs["last_check"] = _SQL_NOW

        fields = []
        values = []
        for k, v in kwargs.items():
            if v is _SQL_NOW:
                fields.append(f"{k} = datetime('now')")
            else:
                fields.append(f"{k} = ?")
                values.append(v)

        values.append(proxy_id)

        async with self._db_lock:
            await self._connection.execute(f"UPDATE proxies SET {', '.join(fields)} WHERE id = ?", values)
            await self._commit_with_retry()

    async def list_proxies(self, status: str | None = None, unassigned_only: bool = False) -> list[ProxyRecord]:
        """List proxies with filters."""
        query = "SELECT * FROM proxies WHERE 1=1"
        params: list[Any] = []

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
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_proxy_map(self) -> dict[str, str]:
        """Get proxy map {account_name: "socks5:host:port:user:pass"} for all accounts with assigned proxy.

        Returns proxy strings from DB that can be used as proxy_override in TelegramAuth.
        """
        query = """
            SELECT a.name, p.protocol, p.host, p.port, p.username, p.password
            FROM accounts a
            JOIN proxies p ON a.proxy_id = p.id
            WHERE a.proxy_id IS NOT NULL AND p.status = 'active'
        """
        async with self._connection.execute(query) as cursor:
            rows = await cursor.fetchall()
            result = {}
            for row in rows:
                proto = row["protocol"] or "socks5"
                parts = [proto, row["host"], str(row["port"])]
                if row["username"] and row["password"]:
                    parts.extend([row["username"], row["password"]])
                result[row["name"]] = ":".join(parts)
            return result

    # ==================== Migration Tracking ====================

    async def start_migration(self, account_id: int) -> int:
        """Record migration start. Returns migration record ID.

        Updates account status to 'migrating' and creates migration record
        in a single transaction to prevent inconsistency if one step fails.

        BUG-4 FIX: Replaced two separate commits (update_account + INSERT)
        with a single atomic transaction under _db_lock.

        Args:
            account_id: Account ID to start migration for.

        Returns:
            Migration record ID.
        """
        async with self._db_lock:
            # Guard: don't start if already migrating (prevents double-migration)
            cursor = await self._connection.execute(
                "UPDATE accounts SET status = ? WHERE id = ? AND status != ?",
                ("migrating", account_id, "migrating"),
            )
            if cursor.rowcount == 0:
                # Account already migrating or doesn't exist — skip INSERT
                # to avoid orphaned migration records
                logger.debug("start_migration skipped for account %d (already migrating)", account_id)
                await self._commit_with_retry()
                # Return existing incomplete migration ID if available
                async with self._connection.execute(
                    "SELECT id FROM migrations WHERE account_id = ? AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
                    (account_id,),
                ) as c:
                    row = await c.fetchone()
                    return row[0] if row else 0
            async with self._connection.execute(
                """
                INSERT INTO migrations (account_id, started_at)
                VALUES (?, ?)
                """,
                (account_id, datetime.now(UTC).isoformat()),
            ) as cursor:
                migration_id = cursor.lastrowid
            await self._commit_with_retry()
            return migration_id

    async def complete_migration(
        self, migration_id: int, success: bool, error_message: str | None = None, profile_path: str | None = None
    ) -> None:
        """Record migration completion.

        Updates both migrations and accounts tables in a single
        transaction to prevent data inconsistency on crash.

        BUG-2 FIX: Wrapped in _db_lock to prevent interleaving
        with concurrent workers.
        """
        async with self._db_lock:
            # FIX-P1: Single transaction for both migration + account update
            await self._connection.execute(
                """
                UPDATE migrations
                SET completed_at = datetime('now'),
                    success = ?,
                    error_message = ?,
                    profile_path = ?
                WHERE id = ?
                """,
                (1 if success else 0, error_message, profile_path, migration_id),
            )

            # Get account_id and update status BEFORE committing
            async with self._connection.execute(
                "SELECT account_id FROM migrations WHERE id = ?", (migration_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    account_id = row["account_id"]
                    new_status = "healthy" if success else "error"
                    await self._connection.execute(
                        "UPDATE accounts SET status = ?, error_message = ? WHERE id = ?",
                        (new_status, error_message, account_id),
                    )

            # Single commit covers both tables
            await self._commit_with_retry()

    async def get_pending_migrations(self) -> list[AccountRecord]:
        """
        Get accounts that need migration (status='pending' or 'migrating').

        Includes 'migrating' status to catch accounts that were interrupted
        mid-migration (e.g. crash, Ctrl+C). Without this, --resume misses
        accounts stuck in 'migrating' state.

        Used for resume after crash.
        """
        query = "SELECT * FROM accounts WHERE status IN ('pending', 'migrating') ORDER BY name"
        async with self._connection.execute(query) as cursor:
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
                    created_at=row["created_at"],
                    fragment_status=row["fragment_status"],
                    web_last_verified=row["web_last_verified"],
                    auth_ttl_days=row["auth_ttl_days"],
                )
                for row in rows
            ]

    async def get_incomplete_migrations(self) -> list[MigrationRecord]:
        """
        Get migrations that started but never completed.

        These indicate crashes during migration.
        """
        async with self._connection.execute(
            """
            SELECT * FROM migrations
            WHERE completed_at IS NULL
            ORDER BY started_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                MigrationRecord(
                    id=row["id"],
                    account_id=row["account_id"],
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    success=bool(row["success"]) if row["success"] is not None else None,
                    error_message=row["error_message"],
                    profile_path=row["profile_path"],
                )
                for row in rows
            ]

    async def reset_interrupted_migrations(self) -> int:
        """
        Reset accounts that were 'migrating' but migration never completed.

        Returns count of reset accounts.
        """
        # Find incomplete migrations
        incomplete = await self.get_incomplete_migrations()

        if not incomplete:
            return 0

        async with self._db_lock:
            count = 0
            for migration in incomplete:
                # Mark migration as failed
                await self._connection.execute(
                    """
                    UPDATE migrations
                    SET completed_at = datetime('now'),
                        success = 0,
                        error_message = 'Interrupted - reset on restart'
                    WHERE id = ?
                    """,
                    (migration.id,),
                )

                # Reset account status to pending
                await self._connection.execute(
                    """
                    UPDATE accounts
                    SET status = 'pending',
                        error_message = 'Previous migration interrupted'
                    WHERE id = ? AND status = 'migrating'
                    """,
                    (migration.account_id,),
                )
                count += 1

            if count > 0:
                await self._commit_with_retry()
                logger.info("Reset %d interrupted migrations", count)

        return count

    async def get_migration_stats(self) -> dict:
        """Get migration statistics using SQL COUNT + GROUP BY."""
        stats = {"total": 0, "pending": 0, "migrating": 0, "healthy": 0, "error": 0, "success_rate": 0.0}

        async with self._connection.execute("SELECT status, COUNT(*) as cnt FROM accounts GROUP BY status") as cursor:
            async for row in cursor:
                status = row["status"]
                if status in stats:
                    stats[status] = row["cnt"]
                stats["total"] += row["cnt"]

        completed = stats["healthy"] + stats["error"]
        if completed > 0:
            stats["success_rate"] = stats["healthy"] / completed * 100

        return stats

    # ==================== Batch Management ====================

    async def start_batch(self, account_names: list[str]) -> str:
        """Start a new migration batch.

        BUG-2 FIX: Wrapped in _db_lock to prevent interleaving
        with concurrent workers during multi-step INSERT operations.

        Args:
            account_names: List of account names in this batch.

        Returns:
            Batch ID (UUID-based).
        """
        async with self._db_lock:
            # FIX D3: Auto-close orphaned batches from previous crashes
            await self._connection.execute(
                "UPDATE batches SET finished_at = ? WHERE finished_at IS NULL", (datetime.now(UTC).isoformat(),)
            )

            batch_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

            async with self._connection.execute(
                """
                INSERT INTO batches (batch_id, total_count)
                VALUES (?, ?)
                """,
                (batch_id, len(account_names)),
            ) as cursor:
                db_id = cursor.lastrowid

            # Create pending migration records linked to batch
            for name in account_names:
                # Find or skip account — caller must ensure accounts exist in DB
                async with self._connection.execute("SELECT id FROM accounts WHERE name = ?", (name,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        await self._connection.execute(
                            """
                            INSERT INTO migrations (account_id, batch_id)
                            VALUES (?, ?)
                            """,
                            (row["id"], db_id),
                        )

            await self._commit_with_retry()
            logger.info("Started batch %s with %d accounts", batch_id, len(account_names))
            return batch_id

    async def get_active_batch(self) -> dict | None:
        """
        Get the most recent unfinished batch.

        Returns:
            Dict with batch info or None.
        """
        async with self._connection.execute(
            """
            SELECT * FROM batches
            WHERE finished_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "id": row["id"],
                    "batch_id": row["batch_id"],
                    "total_count": row["total_count"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                }
            return None

    async def mark_batch_account_completed(self, batch_id: int, account_name: str) -> None:
        """Mark an account's migration as completed within a batch.

        BUG-11 FIX: Inlined account UPDATE SQL instead of calling
        update_account() to avoid double-commit. Single transaction
        for both migration + account status update.

        Args:
            batch_id: Internal batch row ID.
            account_name: Account name.
        """
        async with self._db_lock:
            async with self._connection.execute("SELECT id FROM accounts WHERE name = ?", (account_name,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                account_id = row["id"]

            await self._connection.execute(
                """
                UPDATE migrations
                SET completed_at = datetime('now'), success = 1
                WHERE batch_id = ? AND account_id = ? AND completed_at IS NULL
                """,
                (batch_id, account_id),
            )
            # BUG-11 FIX: Inline account update instead of self.update_account()
            # to avoid double-commit (update_account commits internally)
            await self._connection.execute("UPDATE accounts SET status = ? WHERE id = ?", ("healthy", account_id))
            await self._commit_with_retry()

    async def mark_batch_account_failed(self, batch_id: int, account_name: str, error: str) -> None:
        """Mark an account's migration as failed within a batch.

        BUG-11 FIX: Inlined account UPDATE SQL instead of calling
        update_account() to avoid double-commit. Single transaction
        for both migration + account status update.

        Args:
            batch_id: Internal batch row ID.
            account_name: Account name.
            error: Error message.
        """
        async with self._db_lock:
            async with self._connection.execute("SELECT id FROM accounts WHERE name = ?", (account_name,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                account_id = row["id"]

            await self._connection.execute(
                """
                UPDATE migrations
                SET completed_at = datetime('now'), success = 0, error_message = ?
                WHERE batch_id = ? AND account_id = ? AND completed_at IS NULL
                """,
                (error, batch_id, account_id),
            )
            # BUG-11 FIX: Inline account update instead of self.update_account()
            # to avoid double-commit (update_account commits internally)
            await self._connection.execute(
                "UPDATE accounts SET status = ?, error_message = ? WHERE id = ?", ("error", error, account_id)
            )
            await self._commit_with_retry()

    async def get_batch_pending(self, batch_id: int) -> list[str]:
        """
        Get account names with pending (incomplete) migrations in a batch.

        Args:
            batch_id: Internal batch row ID.

        Returns:
            List of account names.
        """
        async with self._connection.execute(
            """
            SELECT a.name FROM migrations m
            JOIN accounts a ON a.id = m.account_id
            WHERE m.batch_id = ? AND m.completed_at IS NULL
            """,
            (batch_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["name"] for row in rows]

    async def get_batch_failed(self, batch_id: int) -> list[dict]:
        """
        Get failed migrations in a batch.

        Args:
            batch_id: Internal batch row ID.

        Returns:
            List of dicts with account name and error.
        """
        async with self._connection.execute(
            """
            SELECT a.name, m.error_message FROM migrations m
            JOIN accounts a ON a.id = m.account_id
            WHERE m.batch_id = ? AND m.success = 0
            """,
            (batch_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"account": row["name"], "error": row["error_message"]} for row in rows]

    async def get_batch_status(self) -> dict | None:
        """
        Get status summary of the most recent batch.

        Returns:
            Dict with batch status or None if no batch exists.
        """
        async with self._connection.execute(
            """
            SELECT * FROM batches
            ORDER BY started_at DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None

            batch_db_id = row["id"]
            batch_id = row["batch_id"]
            total = row["total_count"]
            started_at = row["started_at"]
            finished_at = row["finished_at"]

        # Count completed/failed/pending
        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND success = 1", (batch_db_id,)
        ) as cursor:
            completed = (await cursor.fetchone())["cnt"]

        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND success = 0", (batch_db_id,)
        ) as cursor:
            failed = (await cursor.fetchone())["cnt"]

        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND completed_at IS NULL", (batch_db_id,)
        ) as cursor:
            pending = (await cursor.fetchone())["cnt"]

        return {
            "has_batch": True,
            "batch_id": batch_id,
            "batch_db_id": batch_db_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "is_finished": finished_at is not None,
        }

    async def get_last_batch(self) -> dict | None:
        """Get the most recent batch (including finished ones)."""
        async with self._connection.execute(
            "SELECT id, batch_id, total_count, started_at, finished_at FROM batches ORDER BY started_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "batch_id": row[1],
                    "total_count": row[2],
                    "started_at": row[3],
                    "finished_at": row[4],
                }
            return None

    async def finish_batch(self, batch_id: int) -> None:
        """
        Mark a batch as finished.

        Args:
            batch_id: Internal batch row ID.
        """
        async with self._db_lock:
            await self._connection.execute("UPDATE batches SET finished_at = datetime('now') WHERE id = ?", (batch_id,))
            await self._commit_with_retry()

    # ==================== Operation Log ====================

    # Max operation_log entries to keep (auto-rotated on insert)
    OPERATION_LOG_MAX_ROWS = 10000

    async def log_operation(
        self,
        account_id: int | None,
        operation: str,
        success: bool,
        error_message: str | None = None,
        details: str | None = None,
    ) -> None:
        """
        Write an entry to the operation log.

        Auto-rotates: deletes oldest entries when exceeding OPERATION_LOG_MAX_ROWS.

        Args:
            account_id: Account ID (optional).
            operation: Operation name (e.g. 'qr_login', 'fragment_auth').
            success: Whether operation succeeded.
            error_message: Error message on failure.
            details: Additional JSON/text details.
        """
        async with self._db_lock:
            await self._connection.execute(
                """
                INSERT INTO operation_log (account_id, operation, success, error_message, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, operation, 1 if success else 0, error_message, details),
            )
            # Auto-rotate: delete oldest entries beyond limit (every 100 inserts)
            async with self._connection.execute("SELECT COUNT(*) as cnt FROM operation_log") as cursor:
                row = await cursor.fetchone()
                if row and row["cnt"] > self.OPERATION_LOG_MAX_ROWS + 100:
                    await self._connection.execute(
                        """
                        DELETE FROM operation_log WHERE id NOT IN (
                            SELECT id FROM operation_log ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (self.OPERATION_LOG_MAX_ROWS,),
                    )
            await self._commit_with_retry()

    async def get_operation_log(
        self,
        account_id: int | None = None,
        operation: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Read operation log entries.

        Args:
            account_id: Filter by account (optional).
            operation: Filter by operation name (optional).
            limit: Max entries to return.

        Returns:
            List of log entry dicts.
        """
        query = "SELECT * FROM operation_log WHERE 1=1"
        params: list[Any] = []

        if account_id is not None:
            query += " AND account_id = ?"
            params.append(account_id)
        if operation is not None:
            query += " AND operation = ?"
            params.append(operation)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "account_id": row["account_id"],
                    "operation": row["operation"],
                    "success": bool(row["success"]) if row["success"] is not None else None,
                    "error_message": row["error_message"],
                    "details": row["details"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
