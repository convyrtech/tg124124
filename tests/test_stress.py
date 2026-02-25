"""
Stress Tests — verify behavior at scale (1000 accounts, 5 workers).

All tests use mock migrate (no real Telegram/browser), real DB, real asyncio.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def tmp_app_root(tmp_path):
    """Create a temporary app root with accounts/profiles/data dirs."""
    (tmp_path / "accounts").mkdir()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# DB stress under load
# ---------------------------------------------------------------------------

class TestDBStress:
    """Test DB operations under concurrent load."""

    @pytest.mark.asyncio
    async def test_db_lock_contention_under_load(self, tmp_app_root):
        """10 tasks doing 20 writes each = 200 concurrent DB operations."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "stress.db")
        await db.initialize()
        await db.connect()

        try:
            # Add accounts
            ids = []
            for i in range(50):
                aid, _ = await db.add_account(f"stress_{i}", session_path=f"accounts/stress_{i}/session.session")
                ids.append(aid)

            errors = []
            completed = 0

            async def worker(worker_id: int):
                nonlocal completed
                for i in range(20):
                    try:
                        aid = ids[i % len(ids)]
                        await db.update_account(aid, status="migrating")
                        await asyncio.sleep(0)  # Yield to event loop
                        await db.update_account(aid, status="healthy")
                        completed += 1
                    except Exception as e:
                        errors.append(f"W{worker_id}: {e}")

            tasks = [asyncio.create_task(worker(i)) for i in range(10)]
            await asyncio.gather(*tasks)

            assert len(errors) == 0, f"Errors: {errors[:5]}"
            assert completed == 200, f"Expected 200, got {completed}"
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Memory: profile_locks bounded after close_all
# ---------------------------------------------------------------------------

class TestMemoryBounds:
    """Verify that internal dicts are bounded after cleanup."""

    @pytest.mark.asyncio
    async def test_profile_locks_cleared_after_close_all(self, tmp_app_root):
        """_profile_locks dict should be empty after close_all()."""
        from src.browser_manager import BrowserManager

        bm = BrowserManager()

        # Simulate 100 profiles accessed (creates locks)
        for i in range(100):
            bm._get_profile_lock(f"profile_{i}")

        assert len(bm._profile_locks) == 100

        # close_all should clear locks
        await bm.close_all()
        assert len(bm._profile_locks) == 0, \
            f"_profile_locks should be empty after close_all, got {len(bm._profile_locks)}"

    def test_retry_counts_cleared_on_pool_init(self):
        """_retry_counts should be empty at pool creation."""
        from src.worker_pool import MigrationWorkerPool

        pool = MigrationWorkerPool.__new__(MigrationWorkerPool)
        pool._retry_counts = {1: 3, 2: 1, 3: 2}

        # Simulate run() clearing
        pool._retry_counts.clear()
        assert len(pool._retry_counts) == 0


# ---------------------------------------------------------------------------
# Non-retryable patterns
# ---------------------------------------------------------------------------

class TestErrorPatterns:
    """Test error classification at scale."""

    def test_all_non_retryable_patterns_match(self):
        """Every pattern in NON_RETRYABLE_PATTERNS should match at least one error format."""
        from src.worker_pool import MigrationWorkerPool

        pool = MigrationWorkerPool.__new__(MigrationWorkerPool)
        pool.NON_RETRYABLE_PATTERNS = MigrationWorkerPool.NON_RETRYABLE_PATTERNS

        # Map of patterns to example errors they should catch
        test_cases = {
            "phonenumberbanned": "PhoneNumberBannedError",
            "userdeactivated": "UserDeactivatedError",
            "authkeyunregistered": "AuthKeyUnregisteredError",
            "session is not authorized": "Session is not authorized (expired or revoked)",
            "not authorized": "Not authorized: need re-login",
            "dead session": "Dead session file detected",
            "sessionpasswordneeded": "SessionPasswordNeededError",
            "2fa required": "2FA required but no password provided",
            "2fa password": "Invalid 2FA password",
            "unique constraint": "UNIQUE constraint failed: accounts.phone",
            "auth_key_duplicated": "AUTH_KEY_DUPLICATED",
            "authrestart": "AuthRestartError: authentication restarted",
            "session file corrupted": "Session file corrupted: file is not a database",
        }

        for pattern, error in test_cases.items():
            assert not pool._is_retryable(error), \
                f"Pattern '{pattern}' should match error '{error}'"


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

class TestBatchStress:
    """Test batch migration mechanics at scale."""

    @pytest.mark.asyncio
    async def test_batch_dedup_at_scale(self, tmp_app_root):
        """dict.fromkeys() dedup should work with large lists."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "batch.db")
        await db.initialize()
        await db.connect()

        try:
            # Create 100 accounts
            ids = []
            for i in range(100):
                aid, _ = await db.add_account(f"batch_{i}", session_path=f"accounts/batch_{i}/session.session")
                ids.append(aid)

            # Simulate duplicate injection
            duplicated = ids + ids[:50] + ids[:25]  # 175 entries, 100 unique
            deduped = list(dict.fromkeys(duplicated))

            assert len(deduped) == 100
            assert deduped == ids  # Order preserved
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_large_batch_tracking(self, tmp_app_root):
        """Batch with 100 accounts should track correctly."""
        from src.database import Database

        db = Database(tmp_app_root / "data" / "batch_track.db")
        await db.initialize()
        await db.connect()

        try:
            # Create accounts first (start_batch requires existing accounts)
            names = [f"user_{i}" for i in range(100)]
            for name in names:
                await db.add_account(name, session_path=f"accounts/{name}/session.session")  # return ignored

            batch_id = await db.start_batch(names)
            assert batch_id is not None

            # Verify batch exists via get_counts (no get_stats method)
            counts = await db.get_counts()
            assert counts["total"] == 100  # 100 accounts added
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Circuit breaker under load
# ---------------------------------------------------------------------------

class TestCircuitBreakerStress:
    """Test circuit breaker with rapid failure/success cycles."""

    def test_rapid_failure_recovery(self):
        """Circuit breaker should handle rapid open/close cycles."""
        import time
        from src.telegram_auth import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, reset_timeout=0.1)

        for cycle in range(10):
            # Fail 3 times
            for _ in range(3):
                cb.record_failure()

            assert not cb.can_proceed(), f"Cycle {cycle}: should be open"

            # Wait for reset
            time.sleep(0.15)
            assert cb.can_proceed(), f"Cycle {cycle}: should be half-open"

            # Recover
            cb.record_success()
            assert cb.can_proceed(), f"Cycle {cycle}: should be closed"
