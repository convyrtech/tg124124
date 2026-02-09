"""Tests for MigrationWorkerPool."""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.database import Database, AccountRecord, ProxyRecord
from src.resource_monitor import ResourceMonitor
from src.telegram_auth import CircuitBreaker, AuthResult
from src.worker_pool import MigrationWorkerPool, PoolResult, AccountResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(
    account_id: int,
    name: str = "Test",
    proxy_id: int = 1,
    session_dir: Path = None,
) -> AccountRecord:
    """Create a fake AccountRecord."""
    session_path = str(session_dir / "session.session") if session_dir else "/tmp/fake/session.session"
    return AccountRecord(
        id=account_id,
        name=name,
        phone=None,
        username=None,
        session_path=session_path,
        proxy_id=proxy_id,
        status="pending",
        last_check=None,
        error_message=None,
        created_at="2026-01-01",
    )


def _make_proxy(proxy_id: int = 1) -> ProxyRecord:
    return ProxyRecord(
        id=proxy_id,
        host="proxy.example.com",
        port=1080,
        username="user",
        password="pass",
        protocol="socks5",
        status="active",
        assigned_account_id=None,
        last_check=None,
        created_at="2026-01-01",
    )


def _make_auth_result(success: bool = True, error: str = None) -> AuthResult:
    return AuthResult(
        success=success,
        profile_name="test_profile" if success else "",
        error=error,
        user_info={"username": "testuser"} if success else None,
    )


def _mock_db(accounts: dict[int, AccountRecord] = None, proxy: ProxyRecord = None):
    """Create a mock Database with get_account, get_proxy, update_account, start_migration, complete_migration."""
    db = AsyncMock(spec=Database)
    accounts = accounts or {}
    db.get_account = AsyncMock(side_effect=lambda aid: accounts.get(aid))
    db.get_proxy = AsyncMock(return_value=proxy or _make_proxy())
    db.update_account = AsyncMock()
    db.start_migration = AsyncMock(return_value=1)
    db.complete_migration = AsyncMock()
    return db


def _always_can_launch():
    """ResourceMonitor that always allows launching."""
    monitor = MagicMock(spec=ResourceMonitor)
    monitor.can_launch_more = MagicMock(return_value=True)
    return monitor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicRun:
    """Basic pool execution tests."""

    @pytest.mark.asyncio
    async def test_all_succeed(self, tmp_path):
        """5 mock accounts all succeed -> success_count=5."""
        # Create session dirs
        accounts = {}
        for i in range(1, 6):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        logs = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=2,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,  # disable batch pauses
                resource_monitor=_always_can_launch(),
                on_log=logs.append,
            )
            result = await pool.run([1, 2, 3, 4, 5])

        assert result.success_count == 5
        assert result.error_count == 0
        assert result.total == 5
        assert mock_migrate.call_count == 5
        assert db.start_migration.call_count == 5
        assert db.complete_migration.call_count == 5

    @pytest.mark.asyncio
    async def test_empty_ids(self):
        """Empty account list -> immediate return."""
        db = _mock_db()
        pool = MigrationWorkerPool(db=db)
        result = await pool.run([])
        assert result.total == 0
        assert result.success_count == 0


class TestShutdown:
    """Graceful shutdown tests."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_new_work(self, tmp_path):
        """request_shutdown mid-run stops processing new accounts."""
        accounts = {}
        for i in range(1, 11):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        call_count = 0

        async def slow_migrate(**kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return _make_auth_result(success=True)

        with patch("src.worker_pool.migrate_account", side_effect=slow_migrate):
            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
            )

            async def stop_after_delay():
                await asyncio.sleep(0.1)
                pool.request_shutdown()

            asyncio.create_task(stop_after_delay())
            result = await pool.run(list(range(1, 11)))

        # Should have processed fewer than all 10
        assert call_count < 10


class TestCircuitBreaker:
    """Circuit breaker integration tests."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_pauses(self, tmp_path):
        """5 consecutive failures trigger circuit breaker."""
        accounts = {}
        for i in range(1, 8):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=0.1)
        logs = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(
                success=False, error="connection_error"
            )

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
                circuit_breaker=breaker,
                on_log=logs.append,
            )
            result = await pool.run([1, 2, 3, 4, 5, 6, 7])

        # Circuit breaker should have opened after 5 failures
        assert breaker.consecutive_failures >= 5
        # Some log should mention circuit breaker
        breaker_logs = [l for l in logs if "Circuit breaker" in l]
        assert len(breaker_logs) > 0


class TestRetry:
    """Retry logic tests."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, tmp_path):
        """Transient error retried up to max_retries."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="RetryMe", session_dir=d)}

        db = _mock_db(accounts)
        call_count = 0

        async def fail_then_succeed(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_auth_result(success=False, error="transient_error")
            return _make_auth_result(success=True)

        with patch("src.worker_pool.migrate_account", side_effect=fail_then_succeed):
            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=2,
                resource_monitor=_always_can_launch(),
            )
            result = await pool.run([1])

        # Should have been called 3 times: initial + 2 retries
        assert call_count == 3
        assert result.success_count == 1


class TestFloodWait:
    """FLOOD_WAIT detection tests."""

    @pytest.mark.asyncio
    async def test_flood_wait_detection(self, tmp_path):
        """FLOOD_WAIT error triggers extended cooldown."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="FloodAcc", session_dir=d)}

        db = _mock_db(accounts)
        logs = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(
                success=False, error="A]wait of FLOOD_WAIT (X seconds)"
            )

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.01, 0.02),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
                on_log=logs.append,
            )
            result = await pool.run([1])

        flood_logs = [l for l in logs if "FLOOD_WAIT" in l]
        assert len(flood_logs) > 0


class TestResourceMonitor:
    """Resource monitor integration tests."""

    @pytest.mark.asyncio
    async def test_resource_monitor_blocks(self, tmp_path):
        """can_launch_more=False -> account skipped after wait."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Blocked", session_dir=d)}

        db = _mock_db(accounts)
        monitor = MagicMock(spec=ResourceMonitor)
        monitor.can_launch_more = MagicMock(return_value=False)
        logs = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=monitor,
                on_log=logs.append,
            )
            result = await pool.run([1])

        # Should be skipped because resources exhausted
        assert result.skipped_count == 1
        assert mock_migrate.call_count == 0


class TestDBStatusUpdates:
    """Verify DB status transitions."""

    @pytest.mark.asyncio
    async def test_status_migrating_then_healthy(self, tmp_path):
        """Successful migration: pending -> migrating -> healthy."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="StatusTest", session_dir=d)}

        db = _mock_db(accounts)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
            )
            await pool.run([1])

        # start_migration sets status="migrating"
        db.start_migration.assert_called_once_with(1)
        # complete_migration marks success
        db.complete_migration.assert_called_once()
        call_kwargs = db.complete_migration.call_args
        assert call_kwargs[1].get("success") is True or call_kwargs[0][1] is True

    @pytest.mark.asyncio
    async def test_status_migrating_then_error(self, tmp_path):
        """Failed migration: pending -> migrating -> error."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="FailTest", session_dir=d)}

        db = _mock_db(accounts)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(
                success=False, error="auth failed"
            )

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
            )
            await pool.run([1])

        db.start_migration.assert_called_once_with(1)
        db.complete_migration.assert_called_once()
        call_args = db.complete_migration.call_args
        assert call_args[1].get("success") is False or call_args[0][1] is False


class TestTimeout:
    """Task timeout tests."""

    @pytest.mark.asyncio
    async def test_migration_timeout(self, tmp_path):
        """Migration exceeding task_timeout is cancelled."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="SlowAcc", session_dir=d)}

        db = _mock_db(accounts)

        async def hang_forever(**kwargs):
            await asyncio.sleep(999)
            return _make_auth_result(success=True)

        with patch("src.worker_pool.migrate_account", side_effect=hang_forever):
            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                task_timeout=0.1,
                resource_monitor=_always_can_launch(),
            )
            result = await pool.run([1])

        assert result.error_count == 1
        assert "Timeout" in result.results[0].error


class TestProgressCallback:
    """Progress callback tests."""

    @pytest.mark.asyncio
    async def test_progress_callback_called(self, tmp_path):
        """on_progress called for each completed account."""
        accounts = {}
        for i in range(1, 4):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        progress_calls = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
                on_progress=lambda c, t, r: progress_calls.append((c, t)),
            )
            await pool.run([1, 2, 3])

        assert len(progress_calls) == 3
        # Completed counts should be 1, 2, 3 (in some order)
        completed_values = sorted(c for c, t in progress_calls)
        assert completed_values == [1, 2, 3]


class TestFragmentMode:
    """Tests for mode='fragment' dispatch."""

    @pytest.mark.asyncio
    async def test_fragment_mode_calls_fragment_account(self, tmp_path):
        """mode='fragment' dispatches to fragment_account instead of migrate_account."""
        accounts = {}
        for i in range(1, 3):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)

        with (
            patch("src.worker_pool.fragment_account", new_callable=AsyncMock) as mock_frag,
            patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate,
        ):
            mock_frag.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
                mode="fragment",
            )
            result = await pool.run([1, 2])

        assert result.success_count == 2
        assert mock_frag.call_count == 2
        assert mock_migrate.call_count == 0

    @pytest.mark.asyncio
    async def test_fragment_mode_skips_migration_tracking(self, tmp_path):
        """mode='fragment' must NOT call start_migration/complete_migration."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Acc1", session_dir=d)}
        db = _mock_db(accounts)

        with patch("src.worker_pool.fragment_account", new_callable=AsyncMock) as mock_frag:
            mock_frag.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
                mode="fragment",
            )
            await pool.run([1])

        # Fragment mode must NOT touch migrations table or account status
        db.start_migration.assert_not_called()
        db.complete_migration.assert_not_called()

    @pytest.mark.asyncio
    async def test_fragment_mode_updates_fragment_status(self, tmp_path):
        """mode='fragment' updates fragment_status on success."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Acc1", session_dir=d)}
        db = _mock_db(accounts)

        with patch("src.worker_pool.fragment_account", new_callable=AsyncMock) as mock_frag:
            mock_frag.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
                mode="fragment",
            )
            await pool.run([1])

        # Check that fragment_status was updated
        db.update_account.assert_any_call(1, fragment_status="authorized")

    @pytest.mark.asyncio
    async def test_web_mode_does_not_call_fragment(self, tmp_path):
        """Default mode='web' uses migrate_account, not fragment_account."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Acc1", session_dir=d)}
        db = _mock_db(accounts)

        with (
            patch("src.worker_pool.fragment_account", new_callable=AsyncMock) as mock_frag,
            patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate,
        ):
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
            )
            await pool.run([1])

        assert mock_migrate.call_count == 1
        assert mock_frag.call_count == 0


class TestSharedBrowserManager:
    """Tests for shared BrowserManager in pool."""

    def test_pool_creates_browser_manager(self):
        """Pool creates a shared BrowserManager on init."""
        db = _mock_db()
        pool = MigrationWorkerPool(db=db)
        assert pool._browser_manager is not None

    @pytest.mark.asyncio
    async def test_browser_manager_passed_to_migrate(self, tmp_path):
        """Shared browser_manager is passed through to migrate_account."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Acc1", session_dir=d)}
        db = _mock_db(accounts)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
            )
            await pool.run([1])

        # Verify browser_manager kwarg was passed
        call_kwargs = mock_migrate.call_args.kwargs
        assert "browser_manager" in call_kwargs
        assert call_kwargs["browser_manager"] is pool._browser_manager
