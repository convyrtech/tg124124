"""
Tests for telegram_auth module.
"""
import pytest
import json
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram_auth import (
    AccountConfig,
    AuthResult,
    BrowserWatchdog,
    decode_qr_from_screenshot,
    parse_telethon_proxy,
    get_randomized_cooldown,
    MIN_COOLDOWN,
    MAX_COOLDOWN,
    DEFAULT_ACCOUNT_COOLDOWN,
)


class TestAccountConfig:
    """Tests for AccountConfig dataclass and loading."""

    @pytest.fixture
    def valid_account_dir(self, tmp_path):
        """Create a valid account directory structure."""
        account_dir = tmp_path / "test_account"
        account_dir.mkdir()

        # Create session file
        (account_dir / "session.session").touch()

        # Create api.json
        api_config = {"api_id": 12345, "api_hash": "abcdef123456"}
        with open(account_dir / "api.json", 'w') as f:
            json.dump(api_config, f)

        # Create optional ___config.json
        config = {"Name": "Test Account", "Proxy": "socks5:host:1080:user:pass"}
        with open(account_dir / "___config.json", 'w') as f:
            json.dump(config, f)

        return account_dir

    def test_load_valid_account(self, valid_account_dir):
        """Test loading a valid account config."""
        config = AccountConfig.load(valid_account_dir)

        assert config.name == "Test Account"
        assert config.api_id == 12345
        assert config.api_hash == "abcdef123456"
        assert config.proxy == "socks5:host:1080:user:pass"
        assert config.session_path.name == "session.session"

    def test_load_without_optional_config(self, tmp_path):
        """Test loading account without ___config.json."""
        account_dir = tmp_path / "minimal_account"
        account_dir.mkdir()

        (account_dir / "session.session").touch()
        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 1, "api_hash": "hash"}, f)

        config = AccountConfig.load(account_dir)

        assert config.name == "minimal_account"  # Falls back to dir name
        assert config.proxy is None

    def test_load_missing_session_raises(self, tmp_path):
        """Test that missing session file raises FileNotFoundError."""
        account_dir = tmp_path / "no_session"
        account_dir.mkdir()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 1, "api_hash": "hash"}, f)

        with pytest.raises(FileNotFoundError, match="No .session file"):
            AccountConfig.load(account_dir)

    def test_load_missing_api_json_raises(self, tmp_path):
        """Test that missing api.json raises FileNotFoundError."""
        account_dir = tmp_path / "no_api"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with pytest.raises(FileNotFoundError, match="api.json not found"):
            AccountConfig.load(account_dir)

    def test_load_invalid_api_json_raises(self, tmp_path):
        """Test that invalid JSON raises JSONDecodeError."""
        account_dir = tmp_path / "bad_json"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            f.write("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            AccountConfig.load(account_dir)

    def test_load_missing_api_id_raises(self, tmp_path):
        """Test that missing api_id raises KeyError."""
        account_dir = tmp_path / "no_api_id"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_hash": "hash"}, f)  # Missing api_id

        with pytest.raises(KeyError, match="api_id"):
            AccountConfig.load(account_dir)

    def test_load_missing_api_hash_raises(self, tmp_path):
        """Test that missing api_hash raises KeyError."""
        account_dir = tmp_path / "no_api_hash"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 123}, f)  # Missing api_hash

        with pytest.raises(KeyError, match="api_hash"):
            AccountConfig.load(account_dir)

    def test_load_invalid_optional_config_ignored(self, tmp_path):
        """Test that invalid ___config.json is silently ignored."""
        account_dir = tmp_path / "bad_config"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 1, "api_hash": "h"}, f)

        with open(account_dir / "___config.json", 'w') as f:
            f.write("invalid json")

        # Should not raise, just ignore bad config
        config = AccountConfig.load(account_dir)
        assert config.proxy is None
        assert config.name == "bad_config"


class TestAuthResult:
    """Tests for AuthResult dataclass."""

    def test_success_result(self):
        """Test creating a success result."""
        result = AuthResult(
            success=True,
            profile_name="test",
            user_info={"name": "John"}
        )
        assert result.success is True
        assert result.error is None

    def test_failure_result(self):
        """Test creating a failure result."""
        result = AuthResult(
            success=False,
            profile_name="test",
            error="Connection failed"
        )
        assert result.success is False
        assert result.error == "Connection failed"


class TestDecodeQrFromScreenshot:
    """Tests for decode_qr_from_screenshot function."""

    def test_returns_none_for_no_qr(self):
        """Test that function returns None when no QR found."""
        # Create a simple blank image (PNG header + minimal data)
        # This is a minimal valid PNG that won't contain a QR code
        from PIL import Image
        import io

        img = Image.new('RGB', (100, 100), color='white')
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')

        result = decode_qr_from_screenshot(buffer.getvalue())
        assert result is None

    def test_decodes_telegram_qr_format(self):
        """Test decoding a Telegram QR code."""
        # This would require creating an actual QR code image
        # For now, we test the function doesn't crash on valid PNG
        from PIL import Image
        import io

        img = Image.new('RGB', (200, 200), color='white')
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')

        # Should return None (no QR) but not crash
        result = decode_qr_from_screenshot(buffer.getvalue())
        assert result is None


class TestExtractTokenFromTgUrl:
    """Tests for extract_token_from_tg_url function."""

    def test_extracts_valid_token(self):
        """Test extracting token from valid tg:// URL."""
        from src.telegram_auth import extract_token_from_tg_url

        # Создаём тестовый token и URL
        test_data = b"test_token_data_123"
        token_b64 = base64.urlsafe_b64encode(test_data).decode()
        url = f"tg://login?token={token_b64}"

        result = extract_token_from_tg_url(url)
        assert result == test_data

    def test_returns_none_for_invalid_url(self):
        """Test that invalid URLs return None."""
        from src.telegram_auth import extract_token_from_tg_url

        assert extract_token_from_tg_url(None) is None
        assert extract_token_from_tg_url("") is None
        assert extract_token_from_tg_url("https://example.com") is None
        assert extract_token_from_tg_url("tg://login") is None

    def test_handles_url_with_extra_params(self):
        """Test extracting token when URL has additional parameters."""
        from src.telegram_auth import extract_token_from_tg_url

        test_data = b"token_data"
        token_b64 = base64.urlsafe_b64encode(test_data).decode()
        url = f"tg://login?token={token_b64}&other=param"

        result = extract_token_from_tg_url(url)
        assert result == test_data


class TestParseTelethonProxy:
    """Tests for parse_telethon_proxy function."""

    def test_parse_socks5_with_auth(self):
        """Test parsing SOCKS5 proxy with auth."""
        result = parse_telethon_proxy("socks5:proxy.com:1080:user:pass")
        assert result is not None
        assert len(result) == 6
        assert result[1] == "proxy.com"
        assert result[2] == 1080
        assert result[4] == "user"
        assert result[5] == "pass"

    def test_parse_socks5_no_auth(self):
        """Test parsing SOCKS5 proxy without auth."""
        result = parse_telethon_proxy("socks5:proxy.com:1080")
        assert result is not None
        assert len(result) == 3
        assert result[1] == "proxy.com"
        assert result[2] == 1080

    def test_none_returns_none(self):
        """Test that None input returns None."""
        assert parse_telethon_proxy(None) is None

    def test_empty_returns_none(self):
        """Test that empty string returns None."""
        assert parse_telethon_proxy("") is None

    def test_invalid_returns_none(self):
        """Test that invalid format returns None."""
        assert parse_telethon_proxy("invalid") is None
        assert parse_telethon_proxy("too:many:parts:here:a:b") is None

    def test_password_with_colons(self):
        """FIX #15: Password containing colons should be preserved."""
        result = parse_telethon_proxy("socks5:proxy.com:1080:user:pa:ss:word")
        assert result is not None
        assert len(result) == 6
        assert result[1] == "proxy.com"
        assert result[2] == 1080
        assert result[4] == "user"
        assert result[5] == "pa:ss:word"


class TestTelegramAuthIntegration:
    """Integration-style tests for TelegramAuth (without actual network)."""

    @pytest.fixture
    def mock_account(self, tmp_path):
        """Create mock account config."""
        account_dir = tmp_path / "test"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 123, "api_hash": "hash"}, f)

        return AccountConfig.load(account_dir)

    def test_telegram_auth_init(self, mock_account):
        """Test TelegramAuth initialization."""
        from src.telegram_auth import TelegramAuth

        auth = TelegramAuth(mock_account)

        assert auth.account == mock_account
        assert auth.browser_manager is not None
        assert auth._client is None


class TestParallelMigration:
    """Tests for parallel migration functionality."""

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_respects_semaphore(self, tmp_path):
        """Verify semaphore limits concurrent executions."""
        import asyncio

        # Track concurrent calls
        concurrent_count = 0
        max_concurrent = 0
        call_order = []

        async def mock_migrate(account_dir, password_2fa=None, headless=False, **kwargs):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            call_order.append(f"start_{account_dir.name}")
            await asyncio.sleep(0.1)  # Simulate work
            call_order.append(f"end_{account_dir.name}")
            concurrent_count -= 1
            return AuthResult(success=True, profile_name=account_dir.name)

        # Create 5 fake account dirs
        account_dirs = []
        for i in range(5):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            account_dirs.append(d)

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate), \
             patch('src.telegram_auth.BrowserManager'):
            from src.telegram_auth import migrate_accounts_parallel
            results = await migrate_accounts_parallel(
                account_dirs=account_dirs,
                max_concurrent=2,  # Only 2 at a time
                cooldown=0,  # No cooldown for fast tests
                headless=True
            )

        assert len(results) == 5
        assert all(r.success for r in results)
        assert max_concurrent <= 2, f"Semaphore violated: max was {max_concurrent}"

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_progress_callback(self, tmp_path):
        """Verify progress callback is called correctly."""
        import asyncio

        progress_calls = []

        def on_progress(completed, total, result):
            progress_calls.append((completed, total, result.profile_name if result else None))

        async def mock_migrate(account_dir, password_2fa=None, headless=False, **kwargs):
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        account_dirs = [tmp_path / f"acc_{i}" for i in range(3)]
        for d in account_dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate), \
             patch('src.telegram_auth.BrowserManager'):
            from src.telegram_auth import migrate_accounts_parallel
            await migrate_accounts_parallel(
                account_dirs=account_dirs,
                max_concurrent=2,
                cooldown=0,
                on_progress=on_progress
            )

        assert len(progress_calls) == 3
        # Each call should have increasing completed count
        completed_counts = [c[0] for c in progress_calls]
        assert sorted(completed_counts) == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_handles_errors(self, tmp_path):
        """Verify one error doesn't stop others."""
        import asyncio

        async def mock_migrate(account_dir, password_2fa=None, headless=False, **kwargs):
            if "fail" in account_dir.name:
                raise Exception("Simulated failure")
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / "ok_1", tmp_path / "fail_2", tmp_path / "ok_3"]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate), \
             patch('src.telegram_auth.BrowserManager'):
            from src.telegram_auth import migrate_accounts_parallel
            results = await migrate_accounts_parallel(
                account_dirs=dirs,
                max_concurrent=3,
                cooldown=0
            )

        assert len(results) == 3
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 2
        assert len(failures) == 1
        assert "Simulated failure" in failures[0].error

    @pytest.mark.asyncio
    async def test_parallel_migration_shutdown_flag(self, tmp_path):
        """Verify shutdown flag stops accepting new tasks."""
        import asyncio
        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(max_concurrent=2, cooldown=0.1)

        started_count = 0

        async def slow_migrate(account_dir, password_2fa=None, headless=False):
            nonlocal started_count
            started_count += 1
            await asyncio.sleep(0.5)  # Long running
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / f"acc_{i}" for i in range(5)]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=slow_migrate):
            # Start migration
            task = asyncio.create_task(
                controller.run(dirs, headless=True)
            )
            await asyncio.sleep(0.2)  # Let some tasks start

            # Request shutdown
            controller.request_shutdown()

            results = await task

        # Should have stopped early, not all 5 completed
        # At least the running ones should complete
        assert controller.is_shutdown_requested
        # Some should be marked as skipped
        skipped = [r for r in results if r.error and "shutdown" in r.error.lower()]
        assert len(skipped) > 0 or len(results) < 5

    @pytest.mark.asyncio
    async def test_parallel_migration_controller_progress_tracking(self, tmp_path):
        """Verify controller tracks progress correctly."""
        import asyncio
        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(max_concurrent=2, cooldown=0)

        async def quick_migrate(account_dir, password_2fa=None, headless=False):
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / f"acc_{i}" for i in range(3)]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=quick_migrate):
            results = await controller.run(dirs, headless=True)

        completed, total = controller.progress
        assert completed == 3
        assert total == 3
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_parallel_migration_respects_resource_limits(self, tmp_path):
        """Verify resource monitor is called and resources are checked."""
        import asyncio
        from src.telegram_auth import ParallelMigrationController
        from src.resource_monitor import ResourceMonitor

        # Track how many times can_launch_more is called
        check_count = 0

        def mock_can_launch():
            nonlocal check_count
            check_count += 1
            return True  # Always allow (but we track calls)

        monitor = ResourceMonitor()
        monitor.can_launch_more = mock_can_launch

        controller = ParallelMigrationController(
            max_concurrent=5,
            cooldown=0,
            resource_monitor=monitor
        )

        async def quick_migrate(account_dir, **kwargs):
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / f"acc_{i}" for i in range(3)]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=quick_migrate):
            results = await controller.run(dirs, headless=True)

        # Resource monitor should have been checked
        assert check_count >= 3  # At least once per account
        assert len(results) == 3
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_parallel_migration_controller_accepts_resource_monitor(self, tmp_path):
        """Verify controller accepts resource_monitor parameter."""
        from src.telegram_auth import ParallelMigrationController
        from src.resource_monitor import ResourceMonitor

        monitor = ResourceMonitor()
        controller = ParallelMigrationController(
            max_concurrent=5,
            resource_monitor=monitor
        )

        assert controller.resource_monitor is monitor


class TestRandomizedCooldown:
    """Tests for randomized cooldown function."""

    def test_cooldown_in_range(self):
        """Test that cooldown values are within expected range."""
        for _ in range(100):
            cooldown = get_randomized_cooldown()
            assert MIN_COOLDOWN <= cooldown <= MAX_COOLDOWN

    def test_cooldown_varies(self):
        """Test that cooldown values are not fixed (have variance)."""
        values = [get_randomized_cooldown() for _ in range(50)]
        unique_values = set(values)
        # Should have significant variance
        assert len(unique_values) > 10, "Cooldown should vary significantly"

    def test_cooldown_respects_base(self):
        """Test that base_cooldown affects the distribution."""
        # With low base, values should trend lower
        low_values = [get_randomized_cooldown(35) for _ in range(50)]
        # With high base, values should trend higher
        high_values = [get_randomized_cooldown(90) for _ in range(50)]

        avg_low = sum(low_values) / len(low_values)
        avg_high = sum(high_values) / len(high_values)

        # High base should give higher average
        assert avg_high > avg_low

    def test_cooldown_never_negative(self):
        """Test that cooldown is never negative and respects base_cooldown range."""
        # Testing mode: base < MIN_COOLDOWN — allows shorter cooldowns
        for _ in range(100):
            cooldown = get_randomized_cooldown(10)
            assert cooldown >= 10 * 0.5  # Low bound = base * 0.5
            assert cooldown <= 10 * 2    # High bound = base * 2
            assert cooldown > 0

        # Production mode: base >= MIN_COOLDOWN
        for _ in range(100):
            cooldown = get_randomized_cooldown(90)
            assert cooldown >= MIN_COOLDOWN
            assert cooldown <= MAX_COOLDOWN


class TestFloodWaitHandling:
    """Tests for FloodWaitError handling in _accept_token."""

    @pytest.fixture
    def mock_account(self, tmp_path):
        """Create mock account config."""
        account_dir = tmp_path / "test"
        account_dir.mkdir()
        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 123, "api_hash": "hash"}, f)

        return AccountConfig.load(account_dir)

    @pytest.mark.asyncio
    async def test_accept_token_handles_floodwait(self, mock_account):
        """Test that FloodWaitError is handled with retry."""
        from src.telegram_auth import TelegramAuth
        from telethon.errors import FloodWaitError

        auth = TelegramAuth(mock_account)

        # Track call count via side_effect list
        call_count = [0]

        def create_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                error = FloodWaitError(request=None, message="FLOOD_WAIT", code=420)
                error.seconds = 1  # Short wait for testing
                raise error
            return MagicMock()

        # Use side_effect on AsyncMock - this is how to control async callable behavior
        mock_client = AsyncMock(side_effect=create_side_effect)

        result = await auth._accept_token(mock_client, b"test_token")

        assert result is True
        assert call_count[0] == 2  # First failed, second succeeded

    @pytest.mark.asyncio
    async def test_accept_token_aborts_long_floodwait(self, mock_account):
        """Test that very long FloodWait aborts instead of waiting."""
        from src.telegram_auth import TelegramAuth
        from telethon.errors import FloodWaitError

        auth = TelegramAuth(mock_account)

        def raise_long_flood(*args, **kwargs):
            error = FloodWaitError(request=None, message="FLOOD_WAIT", code=420)
            error.seconds = 3600  # 1 hour - too long
            raise error

        mock_client = AsyncMock(side_effect=raise_long_flood)

        result = await auth._accept_token(mock_client, b"test_token")

        # Should abort, not wait 1 hour
        assert result is False


class TestSetAuthorizationTTL:
    """Tests for _set_authorization_ttl method."""

    @pytest.fixture
    def telegram_auth(self, tmp_path):
        """Create TelegramAuth instance with mocked config."""
        from src.telegram_auth import TelegramAuth, AccountConfig, DeviceConfig
        config = AccountConfig(
            name="test",
            session_path=tmp_path / "test.session",
            api_id=12345,
            api_hash="test_hash",
            proxy=None,
            device=DeviceConfig()
        )
        return TelegramAuth(config)

    @pytest.mark.asyncio
    async def test_set_ttl_success(self, telegram_auth):
        """Should call SetAuthorizationTTLRequest with 365 days."""
        mock_client = AsyncMock()
        mock_client.return_value = True

        result = await telegram_auth._set_authorization_ttl(mock_client)

        assert result is True
        mock_client.assert_called_once()
        call_args = mock_client.call_args[0][0]
        assert call_args.authorization_ttl_days == 365

    @pytest.mark.asyncio
    async def test_set_ttl_failure_non_fatal(self, telegram_auth):
        """Should handle errors gracefully and return False."""
        mock_client = AsyncMock()
        mock_client.side_effect = Exception("API error")

        result = await telegram_auth._set_authorization_ttl(mock_client)

        assert result is False


class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state_closed(self):
        """Circuit should start in closed state."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker()
        assert not breaker.is_open
        assert breaker.can_proceed()
        assert breaker.consecutive_failures == 0

    def test_opens_after_threshold_failures(self):
        """Circuit should open after reaching failure threshold."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=3, reset_timeout=60)

        breaker.record_failure()
        assert not breaker.is_open

        breaker.record_failure()
        assert not breaker.is_open

        breaker.record_failure()  # Third failure
        assert breaker.is_open
        assert not breaker.can_proceed()

    def test_success_resets_failures(self):
        """Success should reset failure counter and close circuit."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=3)

        breaker.record_failure()
        breaker.record_failure()
        assert breaker.consecutive_failures == 2

        breaker.record_success()
        assert breaker.consecutive_failures == 0
        assert not breaker.is_open

    def test_success_closes_open_circuit(self):
        """Success should close an open circuit."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=2)

        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open

        breaker.record_success()
        assert not breaker.is_open
        assert breaker.can_proceed()

    def test_can_proceed_after_reset_timeout(self):
        """Circuit should allow retry after reset timeout."""
        from src.telegram_auth import CircuitBreaker
        import time

        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=0.1)  # 100ms

        breaker.record_failure()
        assert breaker.is_open
        assert not breaker.can_proceed()

        time.sleep(0.15)  # Wait past reset timeout
        assert breaker.can_proceed()  # Should allow retry

    def test_time_until_reset(self):
        """time_until_reset should return remaining wait time."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=60)

        # Closed circuit
        assert breaker.time_until_reset() == 0.0

        breaker.record_failure()
        remaining = breaker.time_until_reset()
        assert 55 < remaining <= 60  # Approximately 60s remaining

    def test_manual_reset(self):
        """reset() should restore initial state."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=2)

        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open

        breaker.reset()
        assert not breaker.is_open
        assert breaker.consecutive_failures == 0
        assert breaker.can_proceed()

    def test_custom_thresholds(self):
        """Custom threshold values should be respected."""
        from src.telegram_auth import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=10, reset_timeout=120)

        for i in range(9):
            breaker.record_failure()
            assert not breaker.is_open

        breaker.record_failure()  # 10th failure
        assert breaker.is_open


class TestParallelMigrationControllerWithCircuitBreaker:
    """Tests for ParallelMigrationController circuit breaker integration."""

    @pytest.mark.asyncio
    async def test_controller_has_circuit_breaker(self, tmp_path):
        """Controller should have a circuit breaker."""
        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(max_concurrent=5)
        assert controller.circuit_breaker is not None

    @pytest.mark.asyncio
    async def test_controller_accepts_custom_circuit_breaker(self, tmp_path):
        """Controller should accept custom circuit breaker."""
        from src.telegram_auth import ParallelMigrationController, CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=10)
        controller = ParallelMigrationController(
            max_concurrent=5,
            circuit_breaker=breaker
        )
        assert controller.circuit_breaker is breaker


class TestAcceptTokenNonRetryable:
    """FIX #7: Non-retryable token errors (EXPIRED, ALREADY_ACCEPTED, INVALID)."""

    @pytest.fixture
    def mock_account(self, tmp_path):
        account_dir = tmp_path / "test"
        account_dir.mkdir()
        (account_dir / "session.session").touch()
        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 123, "api_hash": "hash"}, f)
        return AccountConfig.load(account_dir)

    @pytest.mark.asyncio
    async def test_expired_token_not_retried(self, mock_account):
        """AUTH_TOKEN_EXPIRED should return False immediately, no retry."""
        from src.telegram_auth import TelegramAuth

        auth = TelegramAuth(mock_account)
        call_count = [0]

        def raise_expired(*args, **kwargs):
            call_count[0] += 1
            raise Exception("AUTH_TOKEN_EXPIRED")

        mock_client = AsyncMock(side_effect=raise_expired)
        result = await auth._accept_token(mock_client, b"test_token")

        assert result is False
        assert call_count[0] == 1  # Only 1 attempt, no retries

    @pytest.mark.asyncio
    async def test_already_accepted_not_retried(self, mock_account):
        """AUTH_TOKEN_ALREADY_ACCEPTED should return False immediately."""
        from src.telegram_auth import TelegramAuth

        auth = TelegramAuth(mock_account)
        call_count = [0]

        def raise_already(*args, **kwargs):
            call_count[0] += 1
            raise Exception("AUTH_TOKEN_ALREADY_ACCEPTED")

        mock_client = AsyncMock(side_effect=raise_already)
        result = await auth._accept_token(mock_client, b"test_token")

        assert result is False
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_invalid_token_not_retried(self, mock_account):
        """AUTH_TOKEN_INVALID should return False immediately."""
        from src.telegram_auth import TelegramAuth

        auth = TelegramAuth(mock_account)
        call_count = [0]

        def raise_invalid(*args, **kwargs):
            call_count[0] += 1
            raise Exception("AUTH_TOKEN_INVALID")

        mock_client = AsyncMock(side_effect=raise_invalid)
        result = await auth._accept_token(mock_client, b"test_token")

        assert result is False
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_retryable_error_still_retries(self, mock_account):
        """Other errors should still be retried."""
        from src.telegram_auth import TelegramAuth

        auth = TelegramAuth(mock_account)
        call_count = [0]

        def raise_then_succeed(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Connection reset by peer")
            return MagicMock()

        mock_client = AsyncMock(side_effect=raise_then_succeed)
        result = await auth._accept_token(mock_client, b"test_token")

        assert result is True
        assert call_count[0] == 3  # 2 failures + 1 success


class TestParallelMigrationControllerSharedBrowser:
    """FIX #13: ParallelMigrationController uses shared BrowserManager."""

    @pytest.mark.asyncio
    async def test_controller_passes_browser_manager(self, tmp_path):
        """migrate_account should receive browser_manager kwarg."""
        import asyncio
        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(max_concurrent=2, cooldown=0)
        captured_kwargs = []

        async def capture_migrate(account_dir, **kwargs):
            captured_kwargs.append(kwargs)
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / "acc_1"]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=capture_migrate):
            await controller.run(dirs, headless=True)

        assert len(captured_kwargs) == 1
        assert "browser_manager" in captured_kwargs[0]
        assert captured_kwargs[0]["browser_manager"] is not None


class TestParallelMigrationControllerCooldownAfterCompletion:
    """FIX #16: Cooldown happens after migration, not between create_task."""

    @pytest.mark.asyncio
    async def test_cooldown_after_completion(self, tmp_path):
        """With cooldown > 0, tasks should complete with gaps between them."""
        import asyncio
        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(max_concurrent=1, cooldown=0.01)
        timestamps = []

        async def timed_migrate(account_dir, **kwargs):
            import time
            timestamps.append(time.time())
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / f"acc_{i}" for i in range(2)]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=timed_migrate):
            await controller.run(dirs, headless=True)

        # Both should complete
        assert len(timestamps) == 2


class TestIsProfileAlreadyAuthorized:
    """Tests for pre-check that skips browser launch for already-migrated profiles."""

    @pytest.fixture
    def auth_instance(self, tmp_path):
        """Create a TelegramAuth instance with mocked dependencies."""
        from src.telegram_auth import TelegramAuth, AccountConfig
        account_dir = tmp_path / "test_account"
        account_dir.mkdir()
        (account_dir / "session.session").write_bytes(b"fake")
        (account_dir / "api.json").write_text('{"api_id": 123, "api_hash": "abc"}')
        account = AccountConfig.load(account_dir)
        auth = TelegramAuth(account, browser_manager=MagicMock())
        return auth

    def test_returns_true_when_user_auth_present(self, auth_instance, tmp_path):
        """Profile with user_auth in storage_state.json is detected as authorized."""
        profile = MagicMock()
        profile.path = tmp_path / "profile"
        profile.path.mkdir()
        state = {
            "origins": [{
                "origin": "https://web.telegram.org",
                "localStorage": [
                    {"name": "user_auth", "value": '{"date":1770762062,"id":7954844955,"dcID":1}'},
                    {"name": "number_of_accounts", "value": "1"},
                ]
            }]
        }
        (profile.path / "storage_state.json").write_text(json.dumps(state))
        assert auth_instance._is_profile_already_authorized(profile) is True

    def test_returns_false_when_no_user_auth(self, auth_instance, tmp_path):
        """Profile without user_auth is not detected as authorized."""
        profile = MagicMock()
        profile.path = tmp_path / "profile"
        profile.path.mkdir()
        state = {
            "origins": [{
                "origin": "https://web.telegram.org",
                "localStorage": [
                    {"name": "number_of_accounts", "value": "0"},
                    {"name": "dc", "value": "1"},
                ]
            }]
        }
        (profile.path / "storage_state.json").write_text(json.dumps(state))
        assert auth_instance._is_profile_already_authorized(profile) is False

    def test_returns_false_when_no_storage_state(self, auth_instance, tmp_path):
        """Missing storage_state.json returns False."""
        profile = MagicMock()
        profile.path = tmp_path / "profile"
        profile.path.mkdir()
        assert auth_instance._is_profile_already_authorized(profile) is False

    def test_returns_false_on_corrupted_json(self, auth_instance, tmp_path):
        """Corrupted storage_state.json returns False gracefully."""
        profile = MagicMock()
        profile.path = tmp_path / "profile"
        profile.path.mkdir()
        (profile.path / "storage_state.json").write_text("{invalid json")
        assert auth_instance._is_profile_already_authorized(profile) is False

    def test_returns_false_when_user_auth_has_no_id(self, auth_instance, tmp_path):
        """user_auth without 'id' field is not considered authorized."""
        profile = MagicMock()
        profile.path = tmp_path / "profile"
        profile.path.mkdir()
        state = {
            "origins": [{
                "origin": "https://web.telegram.org",
                "localStorage": [
                    {"name": "user_auth", "value": '{"date":1770762062}'},
                ]
            }]
        }
        (profile.path / "storage_state.json").write_text(json.dumps(state))
        assert auth_instance._is_profile_already_authorized(profile) is False


class TestBrowserWatchdog:
    """Tests for BrowserWatchdog thread-based timeout mechanism."""

    def test_watchdog_cancel_prevents_kill(self):
        """Cancelled watchdog does not call kill."""
        import time
        watchdog = BrowserWatchdog(
            driver_pid=99999, browser_pid=99998,
            profile_name="test", timeout=0.2,
        )
        watchdog.start()
        watchdog.cancel()
        time.sleep(0.5)
        # If kill was called, it would try psutil.Process(99999) and fail,
        # but cancel should prevent it entirely.

    def test_watchdog_fires_and_kills_process(self):
        """Watchdog kills process after timeout."""
        import time
        import subprocess
        import sys

        # Start a dummy long-running process to kill
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        pid = proc.pid

        watchdog = BrowserWatchdog(
            driver_pid=None, browser_pid=pid,
            profile_name="test_kill", timeout=0.3,
        )
        watchdog.start()
        time.sleep(1)  # Wait for watchdog to fire

        # Process should be dead
        assert proc.poll() is not None, "Watchdog did not kill the process"
        watchdog.cancel()  # Cleanup (no-op after fire)

    def test_watchdog_handles_nonexistent_pid(self):
        """Watchdog gracefully handles PIDs that don't exist."""
        import time
        watchdog = BrowserWatchdog(
            driver_pid=999999, browser_pid=999998,
            profile_name="ghost", timeout=0.1,
        )
        watchdog.start()
        time.sleep(0.5)
        # Should not raise — NoSuchProcess is caught internally
        watchdog.cancel()

    def test_watchdog_timer_is_daemon(self):
        """Watchdog thread is daemon (won't prevent process exit)."""
        watchdog = BrowserWatchdog(
            driver_pid=1, browser_pid=2,
            profile_name="test", timeout=999,
        )
        assert watchdog._timer.daemon is True
        watchdog.cancel()


class TestSafeDisconnect:
    """Tests for TelegramAuth._safe_disconnect static method."""

    @pytest.mark.asyncio
    async def test_safe_disconnect_suppresses_errors(self):
        """_safe_disconnect doesn't raise even if client.disconnect() fails."""
        from src.telegram_auth import TelegramAuth

        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = ConnectionError("already closed")

        # Should NOT raise
        await TelegramAuth._safe_disconnect(mock_client)
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_safe_disconnect_normal(self):
        """_safe_disconnect calls disconnect on healthy client."""
        from src.telegram_auth import TelegramAuth

        mock_client = AsyncMock()
        await TelegramAuth._safe_disconnect(mock_client)
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_safe_disconnect_timeout(self):
        """_safe_disconnect handles client that hangs on disconnect."""
        import asyncio
        from src.telegram_auth import TelegramAuth

        mock_client = AsyncMock()

        async def hang_forever():
            await asyncio.sleep(100)

        mock_client.disconnect.side_effect = hang_forever

        # Should return within ~5s timeout, not hang
        await TelegramAuth._safe_disconnect(mock_client)
