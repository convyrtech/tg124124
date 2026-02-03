"""
Tests for fragment_auth module.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from src.fragment_auth import (
    FragmentAuth,
    FragmentResult,
    _mask_phone,
    FRAGMENT_URL,
    TELEGRAM_SERVICE_USER_ID,
)
from src.telegram_auth import AccountConfig, DeviceConfig


class TestMaskPhone:
    """Tests for phone number masking."""

    def test_mask_full_phone(self):
        assert _mask_phone("+79991234567") == "+799***4567"

    def test_mask_short_phone(self):
        assert _mask_phone("+123") == "***"

    def test_mask_empty(self):
        assert _mask_phone("") == "***"

    def test_mask_none(self):
        assert _mask_phone(None) == "***"

    def test_mask_international(self):
        assert _mask_phone("+380501234567") == "+380***4567"


class TestFragmentResult:
    """Tests for FragmentResult dataclass."""

    def test_success_result(self):
        result = FragmentResult(
            success=True,
            account_name="test",
            telegram_connected=True
        )
        assert result.success
        assert result.account_name == "test"
        assert result.telegram_connected
        assert not result.already_authorized
        assert result.error is None

    def test_already_authorized(self):
        result = FragmentResult(
            success=True,
            account_name="test",
            already_authorized=True,
            telegram_connected=True
        )
        assert result.already_authorized

    def test_failure_result(self):
        result = FragmentResult(
            success=False,
            account_name="test",
            error="Some error"
        )
        assert not result.success
        assert result.error == "Some error"
        assert not result.telegram_connected


class TestFragmentAuth:
    """Tests for FragmentAuth class."""

    @pytest.fixture
    def mock_account(self, tmp_path):
        """Create a mock AccountConfig."""
        session_path = tmp_path / "session.session"
        session_path.touch()
        return AccountConfig(
            name="test_account",
            session_path=session_path,
            api_id=12345,
            api_hash="test_hash",
            proxy="socks5:host:1080:user:pass",
            device=DeviceConfig()
        )

    @pytest.fixture
    def fragment_auth(self, mock_account):
        """Create a FragmentAuth instance with mocked browser manager."""
        browser_manager = MagicMock()
        return FragmentAuth(mock_account, browser_manager)

    def test_init(self, fragment_auth, mock_account):
        """Test FragmentAuth initialization."""
        assert fragment_auth.account == mock_account
        assert fragment_auth._client is None
        assert fragment_auth._verification_code is None

    def test_extract_code_english(self, fragment_auth):
        """Test code extraction from English message."""
        text = "Login code: 12345. Do not give this code to anyone."
        assert fragment_auth._extract_code_from_message(text) == "12345"

    def test_extract_code_russian(self, fragment_auth):
        """Test code extraction from Russian message."""
        text = "Код входа: 67890. Не сообщайте этот код."
        assert fragment_auth._extract_code_from_message(text) == "67890"

    def test_extract_code_generic(self, fragment_auth):
        """Test code extraction with generic pattern."""
        text = "Your code is 54321 for signing in."
        assert fragment_auth._extract_code_from_message(text) == "54321"

    def test_extract_code_6_digits(self, fragment_auth):
        """Test extraction of 6-digit code."""
        text = "Login code: 123456."
        assert fragment_auth._extract_code_from_message(text) == "123456"

    def test_extract_code_no_code(self, fragment_auth):
        """Test extraction when no code present."""
        assert fragment_auth._extract_code_from_message("Hello world") is None

    def test_extract_code_empty(self, fragment_auth):
        """Test extraction from empty/None."""
        assert fragment_auth._extract_code_from_message("") is None
        assert fragment_auth._extract_code_from_message(None) is None

    @pytest.mark.asyncio
    async def test_check_fragment_state_authorized(self, fragment_auth):
        """Test detecting authorized state."""
        page = AsyncMock()
        # Simulate My Assets link found
        page.query_selector = AsyncMock(side_effect=[
            MagicMock(),  # my-assets found
        ])
        state = await fragment_auth._check_fragment_state(page)
        assert state == "authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_not_authorized(self, fragment_auth):
        """Test detecting not_authorized state."""
        page = AsyncMock()
        page.query_selector = AsyncMock(side_effect=[
            None,  # no my-assets
            MagicMock(),  # connect telegram button found
        ])
        state = await fragment_auth._check_fragment_state(page)
        assert state == "not_authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_from_text(self, fragment_auth):
        """Test detecting state from page text."""
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value="Connect TON and Telegram to view your bids")
        state = await fragment_auth._check_fragment_state(page)
        assert state == "not_authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_loading(self, fragment_auth):
        """Test detecting loading state."""
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value="")
        page.title = AsyncMock(return_value="Loading...")
        state = await fragment_auth._check_fragment_state(page)
        assert state == "loading"

    @pytest.mark.asyncio
    async def test_wait_for_code_timeout(self, fragment_auth):
        """Test code wait timeout."""
        code = await fragment_auth._wait_for_code(timeout=1)
        assert code is None

    @pytest.mark.asyncio
    async def test_wait_for_code_received(self, fragment_auth):
        """Test code received before timeout."""
        # Simulate code arrival
        async def set_code():
            await asyncio.sleep(0.1)
            fragment_auth._verification_code = "12345"
            fragment_auth._code_event.set()

        asyncio.create_task(set_code())
        code = await fragment_auth._wait_for_code(timeout=5)
        assert code == "12345"

    @pytest.mark.asyncio
    async def test_human_delay(self, fragment_auth):
        """Test human delay is within bounds."""
        import time
        start = time.monotonic()
        await fragment_auth._human_delay(0.1, 0.2)
        elapsed = time.monotonic() - start
        assert 0.1 <= elapsed <= 0.5  # Some tolerance

    @pytest.mark.asyncio
    async def test_connect_returns_error_on_session_failure(self, fragment_auth):
        """Test that connect returns error when Telethon session fails."""
        with patch.object(
            fragment_auth, '_create_telethon_client',
            side_effect=RuntimeError("Session is not authorized.")
        ):
            result = await fragment_auth.connect(headless=True)
            assert not result.success
            assert "not authorized" in result.error

    @pytest.mark.asyncio
    async def test_connect_already_authorized(self, fragment_auth):
        """Test connect when already authorized on Fragment."""
        # Mock Telethon client
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_me.first_name = "Test"
        mock_me.id = 12345
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.disconnect = AsyncMock()
        # Make client.on() return a proper decorator (not a coroutine)
        mock_client.on = MagicMock(return_value=lambda f: f)
        mock_client.remove_event_handler = MagicMock()

        # Mock browser
        mock_page = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client',
            return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state',
            return_value="authorized"
        ):
            result = await fragment_auth.connect(headless=True)
            assert result.success
            assert result.already_authorized


class TestCodeExtraction:
    """Additional tests for code extraction edge cases."""

    @pytest.fixture
    def auth(self, tmp_path):
        session_path = tmp_path / "s.session"
        session_path.touch()
        account = AccountConfig(
            name="t", session_path=session_path,
            api_id=1, api_hash="h"
        )
        return FragmentAuth(account)

    def test_code_with_extra_text(self, auth):
        msg = "Web Login code: 98765\n\nSomeone tried to log in..."
        assert auth._extract_code_from_message(msg) == "98765"

    def test_code_case_insensitive(self, auth):
        msg = "LOGIN CODE: 11111"
        assert auth._extract_code_from_message(msg) == "11111"

    def test_multiple_numbers_takes_first_code_pattern(self, auth):
        msg = "Login code: 55555. Request from IP 192.168.1.1"
        assert auth._extract_code_from_message(msg) == "55555"

    def test_code_with_colon_no_space(self, auth):
        msg = "Login code:99999"
        assert auth._extract_code_from_message(msg) == "99999"
