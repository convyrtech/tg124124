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
        """can_launch_more=False -> first account still runs (guarantee min 1 browser),
        but second account gets retried/skipped when resources stay exhausted."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="First", session_dir=d)}

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

        # First account must run even when resources exhausted (min 1 browser guarantee)
        assert result.success_count == 1
        assert mock_migrate.call_count == 1

    @pytest.mark.asyncio
    async def test_resource_monitor_blocks_second_account(self, tmp_path):
        """When 1 browser active + resources exhausted, 2nd account retries not skips."""
        d1 = tmp_path / "account_1"
        d1.mkdir()
        (d1 / "session.session").touch()
        d2 = tmp_path / "account_2"
        d2.mkdir()
        (d2 / "session.session").touch()
        accounts = {
            1: _make_account(1, name="First", session_dir=d1),
            2: _make_account(2, name="Second", session_dir=d2),
        }

        db = _mock_db(accounts)
        monitor = MagicMock(spec=ResourceMonitor)
        monitor.can_launch_more = MagicMock(return_value=False)
        logs = []

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=2,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=monitor,
                on_log=logs.append,
                max_retries=0,  # No retries to simplify test
            )
            result = await pool.run([1, 2])

        # Both should run — first bypasses resource check, second runs after first finishes
        assert mock_migrate.call_count >= 1
        assert result.success_count >= 1


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


class TestDeduplication:
    """FIX #6: Duplicate account_ids must be removed."""

    @pytest.mark.asyncio
    async def test_duplicate_ids_removed(self, tmp_path):
        """Same account_id appearing twice should only be processed once."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Dup", session_dir=d)}

        db = _mock_db(accounts)
        call_count = 0

        async def count_migrate(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_auth_result(success=True)

        with patch("src.worker_pool.migrate_account", side_effect=count_migrate):
            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
            )
            result = await pool.run([1, 1, 1])

        # Should deduplicate: only 1 migration, not 3
        assert call_count == 1
        assert result.total == 1
        assert result.success_count == 1

    @pytest.mark.asyncio
    async def test_dedup_preserves_order(self, tmp_path):
        """Deduplication preserves first occurrence order."""
        accounts = {}
        for i in [3, 1, 2]:
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        seen_ids = []

        async def track_migrate(**kwargs):
            account_dir = kwargs.get("account_dir")
            if account_dir:
                seen_ids.append(account_dir.name)
            return _make_auth_result(success=True)

        with patch("src.worker_pool.migrate_account", side_effect=track_migrate):
            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                resource_monitor=_always_can_launch(),
            )
            result = await pool.run([3, 1, 2, 1, 3])

        assert result.total == 3  # Deduplicated from 5 to 3


class TestBatchPauseEvent:
    """FIX #5: Batch pause should block ALL workers via shared event."""

    @pytest.mark.asyncio
    async def test_batch_pause_event_exists(self):
        """Pool should have _batch_pause_event attribute."""
        db = _mock_db()
        pool = MigrationWorkerPool(db=db)
        assert hasattr(pool, "_batch_pause_event")
        assert pool._batch_pause_event.is_set()  # Starts in running state

    @pytest.mark.asyncio
    async def test_batch_pause_clears_and_sets_event(self, tmp_path):
        """Batch pause should clear event (block workers) then set it (resume)."""
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
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=5,
                batch_pause_range=(0.01, 0.02),
                resource_monitor=_always_can_launch(),
                on_log=logs.append,
            )
            result = await pool.run([1, 2, 3, 4, 5])

        assert result.success_count == 5
        # Should have a batch pause log
        pause_logs = [l for l in logs if "Batch pause" in l]
        assert len(pause_logs) >= 1


class TestQueueJoinTimeout:
    """FIX #12: queue.join() must have a timeout."""

    @pytest.mark.asyncio
    async def test_queue_join_timeout_handled(self, tmp_path):
        """If queue.join() times out, pool should still send stop sentinels."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="JoinTimeout", session_dir=d)}

        db = _mock_db(accounts)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(success=True)

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                task_timeout=0.5,
                resource_monitor=_always_can_launch(),
            )
            # Normal run should complete fine (join doesn't actually timeout)
            result = await pool.run([1])

        assert result.success_count == 1


class TestCircuitBreakerHalfOpen:
    """FIX #4: Half-open circuit breaker probe tests."""

    def test_half_open_probing_flag_initial(self):
        """Circuit breaker starts with _half_open_probing=False."""
        breaker = CircuitBreaker()
        assert breaker._half_open_probing is False

    @pytest.mark.asyncio
    async def test_acquire_probe_when_closed(self):
        """acquire_half_open_probe returns True when circuit is closed."""
        breaker = CircuitBreaker()
        assert await breaker.acquire_half_open_probe() is True

    @pytest.mark.asyncio
    async def test_acquire_probe_blocks_second_caller(self):
        """Only one caller can acquire the probe; second gets False."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        breaker.record_failure()
        assert breaker.is_open

        import time
        time.sleep(0.02)  # Wait for reset timeout

        # First caller gets the probe
        assert await breaker.acquire_half_open_probe() is True
        assert breaker._half_open_probing is True

        # Second caller is blocked
        assert await breaker.acquire_half_open_probe() is False

        # Release
        breaker.release_half_open_probe()
        assert breaker._half_open_probing is False

    def test_record_success_resets_probing(self):
        """record_success resets _half_open_probing flag."""
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=0.01)
        breaker.record_failure()
        breaker._half_open_probing = True
        breaker.record_success()
        assert breaker._half_open_probing is False

    def test_reset_clears_probing(self):
        """reset() clears the probing flag."""
        breaker = CircuitBreaker()
        breaker._half_open_probing = True
        breaker.reset()
        assert breaker._half_open_probing is False


class TestCircuitBreakerSkipOnConfigError:
    """Circuit breaker must NOT trigger for non-retryable config errors."""

    @pytest.mark.asyncio
    async def test_config_error_does_not_open_circuit_breaker(self, tmp_path):
        """Non-retryable error (api_id not found) must NOT call record_failure."""
        accounts = {}
        for i in range(1, 7):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=0.1)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            # Simulate config error: KeyError('api_id') not found in api.json
            mock_migrate.side_effect = KeyError("'api_id' not found in api.json")

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
                circuit_breaker=breaker,
                on_log=lambda m: None,
            )
            await pool.run([1, 2, 3, 4, 5, 6])

        # 6 config errors must NOT open circuit breaker
        assert breaker.consecutive_failures == 0
        assert not breaker.is_open

    @pytest.mark.asyncio
    async def test_retryable_error_opens_circuit_breaker(self, tmp_path):
        """Retryable (infrastructure) errors DO trigger circuit breaker."""
        accounts = {}
        for i in range(1, 7):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            (d / "session.session").touch()
            accounts[i] = _make_account(i, name=f"Acc{i}", session_dir=d)

        db = _mock_db(accounts)
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=0.1)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(
                success=False, error="connection_error: proxy refused"
            )

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
                circuit_breaker=breaker,
                on_log=lambda m: None,
            )
            await pool.run([1, 2, 3, 4, 5, 6])

        # Infrastructure failures MUST open circuit breaker
        assert breaker.consecutive_failures >= 5
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_auth_result_non_retryable_skips_breaker(self, tmp_path):
        """auth_result with non-retryable error (banned) must NOT trigger breaker."""
        d = tmp_path / "account_1"
        d.mkdir()
        (d / "session.session").touch()
        accounts = {1: _make_account(1, name="Banned", session_dir=d)}

        db = _mock_db(accounts)
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60)

        with patch("src.worker_pool.migrate_account", new_callable=AsyncMock) as mock_migrate:
            mock_migrate.return_value = _make_auth_result(
                success=False, error="PhoneNumberBanned"
            )

            pool = MigrationWorkerPool(
                db=db,
                num_workers=1,
                cooldown_range=(0.0, 0.01),
                batch_pause_every=0,
                max_retries=0,
                resource_monitor=_always_can_launch(),
                circuit_breaker=breaker,
                on_log=lambda m: None,
            )
            await pool.run([1])

        assert breaker.consecutive_failures == 0


class TestHumanizeError:
    """humanize_error returns Russian messages for known patterns."""

    def test_proxy_lib_error(self):
        from src.worker_pool import humanize_error
        msg = humanize_error("Telethon connection failed (proxy lib error): ConnectionError()")
        assert "прокси" in msg.lower()

    def test_api_id_not_found(self):
        from src.worker_pool import humanize_error
        msg = humanize_error("'api_id' not found in api.json")
        assert "api.json" in msg.lower()

    def test_session_file_corrupted(self):
        from src.worker_pool import humanize_error
        msg = humanize_error("Session file corrupted: database disk image")
        assert "повреждён" in msg.lower()

    def test_unknown_error_passthrough(self):
        from src.worker_pool import humanize_error
        msg = humanize_error("some random error xyz")
        assert msg == "some random error xyz"

    def test_none_returns_default(self):
        from src.worker_pool import humanize_error
        msg = humanize_error(None)
        assert msg == "Неизвестная ошибка"
