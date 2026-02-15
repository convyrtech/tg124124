"""
Pre-delivery hardening tests.

Tests for corrupt session handling, DB concurrent access,
circuit breaker probe lifecycle, and EXE dist structure.
"""
import asyncio
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestCorruptSession:
    """Verify corrupt .session files produce readable errors, not cryptic sqlite3 messages."""

    def _create_corrupt_session(self, tmp_path: Path) -> Path:
        """Create a corrupt .session file (invalid SQLite)."""
        session_file = tmp_path / "corrupt.session"
        session_file.write_bytes(b"THIS IS NOT A SQLITE DATABASE AT ALL")
        return session_file

    @pytest.mark.asyncio
    async def test_telegram_auth_corrupt_session_constructor(self, tmp_path):
        """TelegramClient constructor with corrupt session raises readable RuntimeError."""
        session_file = self._create_corrupt_session(tmp_path)

        # Mock account with corrupt session path
        account = MagicMock()
        account.session_path = session_file
        account.api_id = 12345
        account.api_hash = "test_hash"
        account.proxy = None
        account.device = MagicMock(
            device_model="Test", system_version="1.0",
            app_version="1.0", lang_code="en", system_lang_code="en",
        )

        from src.telegram_auth import TelegramAuth
        auth = TelegramAuth.__new__(TelegramAuth)
        auth.account = account

        with pytest.raises(RuntimeError, match="Session file corrupted"):
            await auth._create_telethon_client()

    @pytest.mark.asyncio
    async def test_fragment_auth_corrupt_session_constructor(self, tmp_path):
        """FragmentAuth with corrupt session raises readable RuntimeError."""
        session_file = self._create_corrupt_session(tmp_path)

        account = MagicMock()
        account.session_path = session_file
        account.api_id = 12345
        account.api_hash = "test_hash"
        account.proxy = None
        account.device = MagicMock(
            device_model="Test", system_version="1.0",
            app_version="1.0", lang_code="en", system_lang_code="en",
        )

        from src.fragment_auth import FragmentAuth
        auth = FragmentAuth.__new__(FragmentAuth)
        auth.account = account

        with pytest.raises(RuntimeError, match="Session file corrupted"):
            await auth._create_telethon_client()


class TestDBConcurrentAccess:
    """Verify multiple async tasks can write to DB without 'database locked' errors."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_no_lock_error(self, tmp_path):
        """5 concurrent tasks writing to DB should not raise 'database locked'."""
        from src.database import Database

        db = Database(tmp_path / "test_concurrent.db")
        await db.initialize()
        await db.connect()

        async def write_account(i: int) -> None:
            await db.add_account(
                name=f"account_{i}",
                session_path=str(tmp_path / f"session_{i}.session"),
            )

        # Run 5 writes concurrently
        tasks = [asyncio.create_task(write_account(i)) for i in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # No exceptions should have occurred
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"Concurrent DB writes failed: {errors}"

        # All 5 accounts should exist
        accounts = await db.list_accounts()
        assert len(accounts) == 5

        await db.close()


class TestProbeLifecycle:
    """Verify circuit breaker probe flag is released on ALL early-return paths."""

    @pytest.mark.asyncio
    async def test_probe_released_on_resource_exhaustion(self):
        """Probe acquired but resources exhausted → probe must be released."""
        from src.telegram_auth import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()  # Open circuit

        # Wait for half-open
        await asyncio.sleep(0.02)

        # Acquire probe
        acquired = await cb.acquire_half_open_probe()
        assert acquired is True
        assert cb._half_open_probing is True

        # Simulate early return — must release
        cb.release_half_open_probe()
        assert cb._half_open_probing is False

    @pytest.mark.asyncio
    async def test_probe_released_on_success(self):
        """record_success() also clears the probe flag."""
        from src.telegram_auth import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        await asyncio.sleep(0.02)
        await cb.acquire_half_open_probe()

        cb.record_success()
        assert cb._half_open_probing is False
        assert cb._is_open is False

    @pytest.mark.asyncio
    async def test_second_worker_blocked_during_probe(self):
        """While one worker probes, another cannot acquire probe."""
        from src.telegram_auth import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        cb.record_failure()
        await asyncio.sleep(0.02)

        # First worker acquires probe
        assert await cb.acquire_half_open_probe() is True
        # Second worker is blocked
        assert await cb.acquire_half_open_probe() is False

        # After release, next worker can acquire
        cb.release_half_open_probe()
        assert await cb.acquire_half_open_probe() is True
        cb.release_half_open_probe()


class TestErrorHumanization:
    """Verify corrupt session error is humanized in worker pool."""

    def test_corrupt_session_error_mapped(self):
        """'Session file corrupted' maps to Russian message."""
        from src.worker_pool import humanize_error

        result = humanize_error("Session file corrupted: file is not a database")
        assert "Файл сессии повреждён" in result

    def test_original_errors_still_work(self):
        """Existing error mappings are not broken."""
        from src.worker_pool import humanize_error

        assert "прокси" in humanize_error("AuthKeyDuplicated").lower()
        assert "пароль" in humanize_error("SessionPasswordNeeded").lower()


class TestEXEDistStructure:
    """Programmatic check that dist/TGWebAuth/ has required files (if built)."""

    def test_dist_structure_if_exists(self):
        """If dist/TGWebAuth/ exists, verify key files are present."""
        dist_dir = Path("dist/TGWebAuth")
        if not dist_dir.exists():
            pytest.skip("EXE not built — dist/TGWebAuth/ not found")

        required = [
            "TGWebAuth.exe",
            "camoufox/camoufox.exe",
        ]
        expected_dirs = [
            "accounts",
            "profiles",
            "camoufox",
        ]

        for f in required:
            assert (dist_dir / f).exists(), f"Missing required file: {f}"

        for d in expected_dirs:
            assert (dist_dir / d).exists(), f"Missing required directory: {d}"
