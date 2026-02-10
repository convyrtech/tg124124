"""
SQLite database for TG Web Auth metadata.
Sessions remain as files for portability.
"""
import sqlite3
import uuid
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
    fragment_status: Optional[str] = None  # None, "authorized"


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


@dataclass
class MigrationRecord:
    """Migration history record."""
    id: int
    account_id: int
    started_at: datetime
    completed_at: Optional[datetime]
    success: Optional[bool]
    error_message: Optional[str]
    profile_path: Optional[str]


class Database:
    """SQLite database manager for TG Web Auth."""

    # Whitelist of allowed fields for update_account to prevent SQL injection
    ALLOWED_ACCOUNT_FIELDS = {
        'name', 'phone', 'username', 'session_path',
        'proxy_id', 'status', 'last_check', 'error_message',
        'fragment_status', 'web_last_verified', 'auth_ttl_days'
    }

    # Whitelist for update_proxy
    ALLOWED_PROXY_FIELDS = {
        'host', 'port', 'username', 'password', 'protocol',
        'status', 'assigned_account_id', 'last_check'
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create database and tables if not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Use sync sqlite3 for initial schema creation
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
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

        logger.info("Database initialized: %s", self.db_path)

    async def connect(self) -> None:
        """Open async connection."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA busy_timeout=30000")

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
                    created_at=row["created_at"],
                    fragment_status=row["fragment_status"],
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
                    created_at=row["created_at"],
                    fragment_status=row["fragment_status"],
                )
                for row in rows
            ]

    async def update_account(self, account_id: int, **kwargs) -> None:
        """Update account fields with SQL injection protection."""
        if not kwargs:
            return

        # Validate field names against whitelist
        invalid_fields = set(kwargs.keys()) - self.ALLOWED_ACCOUNT_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid account fields: {invalid_fields}")

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
        """Assign proxy to account (1:1 binding).

        Raises:
            ValueError: If proxy is already assigned to a different account.
        """
        # Check if proxy is already assigned to a different account
        async with self._connection.execute(
            "SELECT assigned_account_id FROM proxies WHERE id = ?", (proxy_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None and row[0] != account_id:
                raise ValueError(
                    f"Proxy {proxy_id} already assigned to account {row[0]}"
                )

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
        """Update proxy fields with SQL injection protection."""
        if not kwargs:
            return

        # Validate field names against whitelist
        invalid_fields = set(kwargs.keys()) - self.ALLOWED_PROXY_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid proxy fields: {invalid_fields}")

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

    # ==================== Migration Tracking ====================

    async def start_migration(self, account_id: int) -> int:
        """
        Record migration start. Returns migration record ID.

        Updates account status to 'migrating'.
        """
        # Update account status
        await self.update_account(account_id, status="migrating")

        # Create migration record
        async with self._connection.execute(
            """
            INSERT INTO migrations (account_id, started_at)
            VALUES (?, datetime('now'))
            """,
            (account_id,)
        ) as cursor:
            await self._connection.commit()
            return cursor.lastrowid

    async def complete_migration(
        self,
        migration_id: int,
        success: bool,
        error_message: Optional[str] = None,
        profile_path: Optional[str] = None
    ) -> None:
        """
        Record migration completion.

        Updates account status to 'healthy' or 'error'.
        """
        await self._connection.execute(
            """
            UPDATE migrations
            SET completed_at = datetime('now'),
                success = ?,
                error_message = ?,
                profile_path = ?
            WHERE id = ?
            """,
            (1 if success else 0, error_message, profile_path, migration_id)
        )
        await self._connection.commit()

        # Get account_id from migration record
        async with self._connection.execute(
            "SELECT account_id FROM migrations WHERE id = ?",
            (migration_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                account_id = row["account_id"]
                # Update account status
                new_status = "healthy" if success else "error"
                await self.update_account(
                    account_id,
                    status=new_status,
                    error_message=error_message
                )

    async def get_pending_migrations(self) -> List[AccountRecord]:
        """
        Get accounts that need migration (status='pending' or incomplete migration).

        Used for resume after crash.
        """
        # Get accounts with status 'pending' or 'migrating' (interrupted)
        return await self.list_accounts(status="pending")

    async def get_incomplete_migrations(self) -> List[MigrationRecord]:
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
                    profile_path=row["profile_path"]
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
                (migration.id,)
            )

            # Reset account status to pending
            await self._connection.execute(
                """
                UPDATE accounts
                SET status = 'pending',
                    error_message = 'Previous migration interrupted'
                WHERE id = ? AND status = 'migrating'
                """,
                (migration.account_id,)
            )
            count += 1

        if count > 0:
            await self._connection.commit()
            logger.info(f"Reset {count} interrupted migrations")

        return count

    async def get_migration_stats(self) -> dict:
        """Get migration statistics."""
        stats = {
            "total": 0,
            "pending": 0,
            "migrating": 0,
            "healthy": 0,
            "error": 0,
            "success_rate": 0.0
        }

        accounts = await self.list_accounts()
        stats["total"] = len(accounts)

        for acc in accounts:
            if acc.status in stats:
                stats[acc.status] += 1

        completed = stats["healthy"] + stats["error"]
        if completed > 0:
            stats["success_rate"] = stats["healthy"] / completed * 100

        return stats

    # ==================== Batch Management ====================

    async def start_batch(self, account_names: list[str]) -> str:
        """
        Start a new migration batch.

        Args:
            account_names: List of account names in this batch.

        Returns:
            Batch ID (UUID-based).
        """
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

        async with self._connection.execute(
            """
            INSERT INTO batches (batch_id, total_count)
            VALUES (?, ?)
            """,
            (batch_id, len(account_names))
        ) as cursor:
            db_id = cursor.lastrowid

        # Create pending migration records linked to batch
        for name in account_names:
            # Find or skip account — caller must ensure accounts exist in DB
            async with self._connection.execute(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    await self._connection.execute(
                        """
                        INSERT INTO migrations (account_id, batch_id)
                        VALUES (?, ?)
                        """,
                        (row["id"], db_id)
                    )

        await self._connection.commit()
        logger.info("Started batch %s with %d accounts", batch_id, len(account_names))
        return batch_id

    async def get_active_batch(self) -> Optional[dict]:
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

    async def mark_batch_account_completed(
        self, batch_id: int, account_name: str
    ) -> None:
        """
        Mark an account's migration as completed within a batch.

        Args:
            batch_id: Internal batch row ID.
            account_name: Account name.
        """
        async with self._connection.execute(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        ) as cursor:
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
            (batch_id, account_id)
        )
        await self.update_account(account_id, status="healthy")
        await self._connection.commit()

    async def mark_batch_account_failed(
        self, batch_id: int, account_name: str, error: str
    ) -> None:
        """
        Mark an account's migration as failed within a batch.

        Args:
            batch_id: Internal batch row ID.
            account_name: Account name.
            error: Error message.
        """
        async with self._connection.execute(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        ) as cursor:
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
            (error, batch_id, account_id)
        )
        await self.update_account(account_id, status="error", error_message=error)
        await self._connection.commit()

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
            (batch_id,)
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
            (batch_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"account": row["name"], "error": row["error_message"]}
                for row in rows
            ]

    async def get_batch_status(self) -> Optional[dict]:
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
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND success = 1",
            (batch_db_id,)
        ) as cursor:
            completed = (await cursor.fetchone())["cnt"]

        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND success = 0",
            (batch_db_id,)
        ) as cursor:
            failed = (await cursor.fetchone())["cnt"]

        async with self._connection.execute(
            "SELECT COUNT(*) as cnt FROM migrations WHERE batch_id = ? AND completed_at IS NULL",
            (batch_db_id,)
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

    async def finish_batch(self, batch_id: int) -> None:
        """
        Mark a batch as finished.

        Args:
            batch_id: Internal batch row ID.
        """
        await self._connection.execute(
            "UPDATE batches SET finished_at = datetime('now') WHERE id = ?",
            (batch_id,)
        )
        await self._connection.commit()

    # ==================== Operation Log ====================

    async def log_operation(
        self,
        account_id: Optional[int],
        operation: str,
        success: bool,
        error_message: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """
        Write an entry to the operation log.

        Args:
            account_id: Account ID (optional).
            operation: Operation name (e.g. 'qr_login', 'fragment_auth').
            success: Whether operation succeeded.
            error_message: Error message on failure.
            details: Additional JSON/text details.
        """
        await self._connection.execute(
            """
            INSERT INTO operation_log (account_id, operation, success, error_message, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, operation, 1 if success else 0, error_message, details)
        )
        await self._connection.commit()

    async def get_operation_log(
        self,
        account_id: Optional[int] = None,
        operation: Optional[str] = None,
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
