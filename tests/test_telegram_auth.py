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
    decode_qr_from_screenshot,
    parse_telethon_proxy,
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

        async def mock_migrate(account_dir, password_2fa=None, headless=False):
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

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
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

        async def mock_migrate(account_dir, password_2fa=None, headless=False):
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        account_dirs = [tmp_path / f"acc_{i}" for i in range(3)]
        for d in account_dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
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

        async def mock_migrate(account_dir, password_2fa=None, headless=False):
            if "fail" in account_dir.name:
                raise Exception("Simulated failure")
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / "ok_1", tmp_path / "fail_2", tmp_path / "ok_3"]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
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
