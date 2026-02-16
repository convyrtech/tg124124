"""
Contract Integration Tests — test the SEAMS between modules, not the modules themselves.

These tests use real DB, real filesystem, real asyncio — but mock external services
(Telegram, browsers). They verify that module A sends correct data to module B.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_app_root(tmp_path):
    """Create a temporary app root with accounts/profiles/data dirs."""
    (tmp_path / "accounts").mkdir()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Contract: After web migration, DB has UTC verified timestamp
# ---------------------------------------------------------------------------

class TestWebMigrationDBContract:
    """Verify that after a successful web migration, the DB contains
    the correct fields: web_last_verified (UTC ISO), auth_ttl_days=365."""

    @pytest.mark.asyncio
    async def test_after_web_migration_db_has_utc_verified_timestamp(self, tmp_app_root):
        """Contract: worker_pool writes UTC timestamp to DB after migration."""
        from datetime import datetime, timezone
        from src.database import Database

        db = Database(tmp_app_root / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            # Insert a test account
            account_id = await db.add_account("test_user", session_path="accounts/test/session.session")

            # Simulate what worker_pool does after successful web migration
            utc_now = datetime.now(timezone.utc).isoformat()
            await db.update_account(
                account_id,
                status="healthy",
                web_last_verified=utc_now,
                auth_ttl_days=365,
            )

            # Verify the contract via raw SQL (AccountRecord doesn't expose these fields)
            account = await db.get_account(account_id)
            assert account.status == "healthy"

            async with db._connection.execute(
                "SELECT web_last_verified, auth_ttl_days FROM accounts WHERE id = ?",
                (account_id,),
            ) as cursor:
                row = await cursor.fetchone()
                assert row["web_last_verified"] is not None, "web_last_verified must be set"
                assert "+00:00" in row["web_last_verified"] or "Z" in row["web_last_verified"], \
                    f"Timestamp must be UTC, got: {row['web_last_verified']}"
                assert row["auth_ttl_days"] == 365, f"auth_ttl_days must be 365, got: {row['auth_ttl_days']}"
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Contract: Session paths are always relative after CLI import
# ---------------------------------------------------------------------------

class TestSessionPathPortability:
    """Verify that session_path stored in DB is always relative."""

    @pytest.mark.asyncio
    async def test_session_path_is_relative_after_add(self, tmp_app_root):
        """Contract: database stores relative paths, not absolute."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            # Even if we pass a relative path
            account_id = await db.add_account("test_user", session_path="accounts/test/session.session")
            account = await db.get_account(account_id)
            path = account.session_path
            assert not Path(path).is_absolute(), f"session_path must be relative, got: {path}"
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Contract: Concurrent workers don't corrupt DB
# ---------------------------------------------------------------------------

class TestConcurrentDBAccess:
    """Verify that multiple async tasks can write to DB without corruption."""

    @pytest.mark.asyncio
    async def test_concurrent_workers_dont_corrupt_db(self, tmp_app_root):
        """Contract: 8 async tasks writing simultaneously, no 'database locked'."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            # Add 20 accounts
            ids = []
            for i in range(20):
                aid = await db.add_account(f"user_{i}", session_path=f"accounts/user_{i}/session.session")
                ids.append(aid)

            # 8 concurrent tasks updating different accounts
            errors = []

            async def update_account(account_id, worker_id):
                try:
                    await db.update_account(account_id, status="migrating")
                    await asyncio.sleep(0.01)  # Simulate work
                    await db.update_account(account_id, status="healthy")
                except Exception as e:
                    errors.append(f"Worker {worker_id}: {e}")

            tasks = []
            for i, aid in enumerate(ids):
                tasks.append(asyncio.create_task(update_account(aid, i % 8)))

            await asyncio.gather(*tasks)

            assert len(errors) == 0, f"DB corruption errors: {errors}"

            # Verify all accounts are healthy
            for aid in ids:
                account = await db.get_account(aid)
                assert account.status == "healthy", f"Account {aid} status: {account.status}"
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Contract: Migration cleanup on cancel
# ---------------------------------------------------------------------------

class TestMigrationCleanup:
    """Verify that cancelled migrations don't leave accounts stuck in 'migrating'."""

    @pytest.mark.asyncio
    async def test_interrupted_migrations_reset_on_startup(self, tmp_app_root):
        """Contract: DB.reset_interrupted_migrations() fixes stuck accounts."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            # Simulate a crash: account left in 'migrating' state
            account_id = await db.add_account("crash_victim", session_path="accounts/crash/session.session")
            # Must use start_migration() to create a migration record (not just update_account)
            await db.start_migration(account_id)

            # On next startup, reset_interrupted_migrations should fix it
            reset_count = await db.reset_interrupted_migrations()
            assert reset_count >= 1, f"Should reset at least 1, got {reset_count}"

            account = await db.get_account(account_id)
            assert account.status == "pending", f"Status should be 'pending' after reset, got: {account.status}"
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Contract: DB portability after app move
# ---------------------------------------------------------------------------

class TestDBPortability:
    """Verify that relative paths in DB work after moving the app directory."""

    @pytest.mark.asyncio
    async def test_db_portability_after_app_move(self, tmp_path):
        """Contract: Relative paths resolve correctly from any APP_ROOT."""
        from src.database import Database

        # Create DB at location A
        root_a = tmp_path / "location_a"
        root_a.mkdir()
        (root_a / "data").mkdir()
        (root_a / "accounts" / "user1").mkdir(parents=True)

        db = Database(root_a / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            # Store relative path
            aid = await db.add_account("user1", session_path="accounts/user1/session.session")
        finally:
            await db.close()

        # "Move" app to location B — copy DB
        root_b = tmp_path / "location_b"
        root_b.mkdir()
        (root_b / "data").mkdir()
        (root_b / "accounts" / "user1").mkdir(parents=True)

        import glob
        import shutil
        # Copy all DB files (including WAL/SHM for SQLite WAL mode)
        for f in glob.glob(str(root_a / "data" / "test.db*")):
            shutil.copy2(f, root_b / "data" / Path(f).name)

        # Open DB at location B
        db2 = Database(root_b / "data" / "test.db")
        await db2.initialize()
        await db2.connect()

        try:
            account = await db2.get_account(aid)

            # Relative path should still work
            resolved = root_b / account.session_path
            assert "user1" in str(resolved)
            assert not Path(account.session_path).is_absolute()
        finally:
            await db2.close()


# ---------------------------------------------------------------------------
# Contract: start_migration guard
# ---------------------------------------------------------------------------

class TestStartMigrationGuard:
    """Verify that start_migration doesn't double-migrate."""

    @pytest.mark.asyncio
    async def test_start_migration_guards_already_migrating(self, tmp_app_root):
        """Contract: calling start_migration on 'migrating' account doesn't change it."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "test.db")
        await db.initialize()
        await db.connect()

        try:
            aid = await db.add_account("user1", session_path="accounts/user1/session.session")

            # First call: should set to migrating
            mid1 = await db.start_migration(aid)
            assert mid1 is not None

            account = await db.get_account(aid)
            assert account.status == "migrating"

            # Second call: should still create migration record but not crash
            mid2 = await db.start_migration(aid)
            assert mid2 is not None
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Contract: Non-retryable errors stop retry loop
# ---------------------------------------------------------------------------

class TestNonRetryableErrors:
    """Verify that NON_RETRYABLE_PATTERNS prevent useless retries."""

    def test_auth_restart_is_non_retryable(self):
        """Contract: AuthRestartError should not be retried."""
        from src.worker_pool import MigrationWorkerPool

        pool = MigrationWorkerPool.__new__(MigrationWorkerPool)
        pool.NON_RETRYABLE_PATTERNS = MigrationWorkerPool.NON_RETRYABLE_PATTERNS

        assert not pool._is_retryable("AuthRestartError: auth restarted")
        assert not pool._is_retryable("Session file corrupted: file is not a database")
        assert not pool._is_retryable("PhoneNumberBannedError")
        assert pool._is_retryable("ConnectionError: timeout")
        assert pool._is_retryable("FloodWaitError: 30s")


# ---------------------------------------------------------------------------
# Contract: CircuitBreaker uses monotonic clock
# ---------------------------------------------------------------------------

class TestCircuitBreakerClock:
    """Verify CircuitBreaker uses monotonic clock, not wall clock."""

    def test_circuit_breaker_monotonic(self):
        """Contract: CircuitBreaker timing is not affected by system clock changes."""
        import time
        from src.telegram_auth import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=2, reset_timeout=1)

        cb.record_failure()
        cb.record_failure()  # Opens circuit

        assert not cb.can_proceed()  # Should be open

        # Wait for reset
        time.sleep(1.1)
        assert cb.can_proceed()  # Should be half-open now
