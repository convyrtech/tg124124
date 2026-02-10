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
    async def test_human_delay(self, fragment_auth):
        """Test human delay is within bounds."""
        import time
        start = time.monotonic()
        await fragment_auth._human_delay(0.1, 0.2)
        elapsed = time.monotonic() - start
        assert 0.1 <= elapsed <= 0.5  # Some tolerance

    # --- _check_fragment_state tests (new evaluate-based approach) ---

    @pytest.mark.asyncio
    async def test_check_fragment_state_authorized_via_js(self, fragment_auth):
        """Test detecting authorized state via Aj.state.unAuth === false."""
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=False)  # Aj.state.unAuth = false
        state = await fragment_auth._check_fragment_state(page)
        assert state == "authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_not_authorized_via_js(self, fragment_auth):
        """Test detecting not_authorized state via Aj.state.unAuth === true."""
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=True)  # Aj.state.unAuth = true
        state = await fragment_auth._check_fragment_state(page)
        assert state == "not_authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_fallback_login_btn(self, fragment_auth):
        """Test fallback: detect not_authorized via button.login-link presence."""
        page = AsyncMock()
        # First evaluate returns None (Aj not loaded), second returns True (button exists)
        page.evaluate = AsyncMock(side_effect=[None, True])
        state = await fragment_auth._check_fragment_state(page)
        assert state == "not_authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_fallback_logout_link(self, fragment_auth):
        """Test fallback: detect authorized via .logout-link presence."""
        page = AsyncMock()
        # Aj not loaded, no login-link CSS, no login-link text fallback, .logout-link exists
        page.evaluate = AsyncMock(side_effect=[None, False, False, True])
        state = await fragment_auth._check_fragment_state(page)
        assert state == "authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_cookie_fallback(self, fragment_auth):
        """Test detecting authorized state via stel_ssid cookie."""
        page = AsyncMock()
        # Aj not loaded, no login-link CSS, no login-link text, no logout-link CSS, no logout-link text
        page.evaluate = AsyncMock(side_effect=[None, False, False, False, False])
        # But stel_ssid cookie exists
        mock_context = MagicMock()
        mock_context.cookies = AsyncMock(return_value=[
            {"name": "stel_ssid", "value": "abc123", "domain": "fragment.com"}
        ])
        page.context = mock_context
        state = await fragment_auth._check_fragment_state(page)
        assert state == "authorized"

    @pytest.mark.asyncio
    async def test_check_fragment_state_loading(self, fragment_auth):
        """Test loading state when nothing is detected (no cookies either)."""
        page = AsyncMock()
        # Aj not loaded, no login-link CSS, no login-link text, no logout-link CSS, no logout-link text
        page.evaluate = AsyncMock(side_effect=[None, False, False, False, False])
        mock_context = MagicMock()
        mock_context.cookies = AsyncMock(return_value=[])
        page.context = mock_context
        state = await fragment_auth._check_fragment_state(page)
        assert state == "loading"

    @pytest.mark.asyncio
    async def test_check_fragment_state_exception(self, fragment_auth):
        """Test unknown state on exception."""
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("page crashed"))
        state = await fragment_auth._check_fragment_state(page)
        assert state == "unknown"

    # --- _open_oauth_popup tests ---

    @pytest.mark.asyncio
    async def test_open_oauth_popup(self, fragment_auth):
        """Test popup is opened by clicking button.login-link."""
        page = AsyncMock()
        mock_popup = AsyncMock()
        mock_popup.url = "https://oauth.telegram.org/auth?..."
        mock_popup.wait_for_load_state = AsyncMock()

        async def _return_popup():
            return mock_popup

        page.wait_for_event = MagicMock(return_value=_return_popup())
        page.click = AsyncMock()

        popup = await fragment_auth._open_oauth_popup(page)
        assert popup == mock_popup
        page.wait_for_event.assert_called_once_with('popup', timeout=20000)
        page.click.assert_awaited_once_with('button.login-link')
        mock_popup.wait_for_load_state.assert_awaited_once_with('domcontentloaded')

    # --- _submit_phone_on_popup tests ---

    @pytest.mark.asyncio
    async def test_submit_phone_on_popup_success(self, fragment_auth):
        """Test phone submission on OAuth popup - success case."""
        popup = AsyncMock()
        popup.evaluate = AsyncMock()
        popup.click = AsyncMock()
        popup.wait_for_selector = AsyncMock()  # success — form appeared

        result = await fragment_auth._submit_phone_on_popup(popup, "79991234567")
        assert result is True

        # Verify evaluate was called with phone
        popup.evaluate.assert_awaited()
        call_args = popup.evaluate.call_args_list[0]
        assert "+79991234567" in str(call_args)

        # Verify submit button was clicked
        popup.click.assert_awaited_once_with('form#send-form button[type="submit"]')

    @pytest.mark.asyncio
    async def test_submit_phone_on_popup_already_formatted(self, fragment_auth):
        """Test phone submission when phone already has + prefix."""
        popup = AsyncMock()
        popup.evaluate = AsyncMock()
        popup.click = AsyncMock()
        popup.wait_for_selector = AsyncMock()

        result = await fragment_auth._submit_phone_on_popup(popup, "+79991234567")
        assert result is True
        call_args = popup.evaluate.call_args_list[0]
        # Should not double the +
        assert "++79991234567" not in str(call_args)

    @pytest.mark.asyncio
    async def test_submit_phone_on_popup_error(self, fragment_auth):
        """Test phone submission failure with error alert."""
        popup = AsyncMock()
        popup.evaluate = AsyncMock(side_effect=[
            None,  # first call: set phone value
            "Invalid phone number",  # second call: get error alert
        ])
        popup.click = AsyncMock()
        popup.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

        result = await fragment_auth._submit_phone_on_popup(popup, "invalid")
        assert result is False

    @pytest.mark.asyncio
    async def test_submit_phone_on_popup_empty_phone(self, fragment_auth):
        """Test phone submission rejects empty phone."""
        popup = AsyncMock()
        assert await fragment_auth._submit_phone_on_popup(popup, "") is False
        assert await fragment_auth._submit_phone_on_popup(popup, "   ") is False

    # --- _check_popup_already_logged_in tests ---

    @pytest.mark.asyncio
    async def test_check_popup_already_logged_in_true(self, fragment_auth):
        """Test detecting already-logged-in oauth popup (ACCEPT/DECLINE)."""
        popup = AsyncMock()
        popup.evaluate = AsyncMock(side_effect=[
            False,  # no login-phone element
            "LOG OUT\nfragment.com requests access\nDECLINE\nACCEPT",
        ])
        result = await fragment_auth._check_popup_already_logged_in(popup)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_popup_already_logged_in_false(self, fragment_auth):
        """Test normal phone-input popup is not flagged as already logged in."""
        popup = AsyncMock()
        popup.evaluate = AsyncMock(return_value=True)  # has login-phone
        result = await fragment_auth._check_popup_already_logged_in(popup)
        assert result is False

    # --- _accept_existing_session tests ---

    @pytest.mark.asyncio
    async def test_accept_existing_session_success(self, fragment_auth):
        """Test clicking ACCEPT via JS evaluate on existing session popup."""
        popup = AsyncMock()
        # JS evaluate finds and clicks ACCEPT, returns 'clicked:ACCEPT'
        popup.evaluate = AsyncMock(return_value="clicked:ACCEPT")
        result = await fragment_auth._accept_existing_session(popup)
        assert result is True

    @pytest.mark.asyncio
    async def test_accept_existing_session_via_get_by_text(self, fragment_auth):
        """Test fallback to get_by_text when JS evaluate returns None."""
        popup = AsyncMock()
        # JS evaluate returns None (no element found via JS)
        # Then get_by_text("ACCEPT") finds it
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = AsyncMock()
        mock_locator.first.click = AsyncMock()
        popup.evaluate = AsyncMock(return_value=None)
        popup.get_by_text = MagicMock(return_value=mock_locator)
        result = await fragment_auth._accept_existing_session(popup)
        assert result is True

    @pytest.mark.asyncio
    async def test_accept_existing_session_failure(self, fragment_auth):
        """Test returns False when no accept button found anywhere."""
        popup = AsyncMock()
        # JS evaluate returns None (no element found)
        # get_by_text also returns count 0
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=0)
        popup.get_by_text = MagicMock(return_value=mock_locator)
        # Use side_effect to return None for JS click, then HTML for debug
        popup.evaluate = AsyncMock(side_effect=[None, "<p>No buttons</p>"])
        result = await fragment_auth._accept_existing_session(popup)
        assert result is False

    # --- connect with existing session test ---

    @pytest.mark.asyncio
    async def test_connect_existing_oauth_session(self, fragment_auth):
        """Test connect flow when oauth popup shows ACCEPT (already logged in)."""
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_me.first_name = "Test"
        mock_me.id = 12345
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        mock_page = AsyncMock()
        mock_popup = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state', side_effect=["not_authorized", "authorized"]
        ), patch.object(
            fragment_auth, '_open_oauth_popup', return_value=mock_popup
        ), patch.object(
            fragment_auth, '_check_popup_already_logged_in', return_value=True
        ), patch.object(
            fragment_auth, '_accept_existing_session', return_value=True
        ), patch.object(
            fragment_auth, '_wait_for_fragment_auth', return_value=True
        ), patch.object(
            fragment_auth, '_human_delay', return_value=None
        ) as mock_delay, patch.object(
            fragment_auth, '_submit_phone_on_popup'
        ) as mock_submit, patch.object(
            fragment_auth, '_confirm_via_telethon'
        ) as mock_confirm:
            result = await fragment_auth.connect(headless=True)
            assert result.success
            assert result.telegram_connected
            # Phone submission and Telethon confirmation should NOT be called
            mock_submit.assert_not_called()
            mock_confirm.assert_not_called()

    # --- _confirm_via_telethon tests ---

    @pytest.mark.asyncio
    async def test_confirm_via_telethon_button(self, fragment_auth):
        """Test confirmation via inline button click."""
        client = MagicMock()
        handlers = []

        def mock_on(event_filter):
            def decorator(func):
                handlers.append(func)
                return func
            return decorator

        client.on = mock_on
        client.remove_event_handler = MagicMock()

        async def simulate_button_message():
            await asyncio.sleep(0.05)
            # Create mock message with Confirm button
            mock_btn = MagicMock()
            mock_btn.text = "Confirm"
            mock_msg = MagicMock()
            mock_msg.buttons = [[mock_btn]]
            mock_msg.click = AsyncMock()

            mock_event = MagicMock()
            mock_event.message = mock_msg
            mock_event.raw_text = ""

            for h in handlers:
                await h(mock_event)

        asyncio.create_task(simulate_button_message())
        result = await fragment_auth._confirm_via_telethon(client, timeout=5)
        assert result is True
        client.remove_event_handler.assert_called()

    @pytest.mark.asyncio
    async def test_confirm_via_telethon_accept_button(self, fragment_auth):
        """Test confirmation via Accept button (Russian)."""
        client = MagicMock()
        handlers = []

        def mock_on(event_filter):
            def decorator(func):
                handlers.append(func)
                return func
            return decorator

        client.on = mock_on
        client.remove_event_handler = MagicMock()

        async def simulate():
            await asyncio.sleep(0.05)
            mock_btn = MagicMock()
            mock_btn.text = "Подтвердить"
            mock_msg = MagicMock()
            mock_msg.buttons = [[mock_btn]]
            mock_msg.click = AsyncMock()

            mock_event = MagicMock()
            mock_event.message = mock_msg
            mock_event.raw_text = ""
            for h in handlers:
                await h(mock_event)

        asyncio.create_task(simulate())
        result = await fragment_auth._confirm_via_telethon(client, timeout=5)
        assert result is True

    @pytest.mark.asyncio
    async def test_confirm_via_telethon_text_code(self, fragment_auth):
        """Test confirmation via text code (no buttons)."""
        client = MagicMock()
        handlers = []

        def mock_on(event_filter):
            def decorator(func):
                handlers.append(func)
                return func
            return decorator

        client.on = mock_on
        client.remove_event_handler = MagicMock()

        async def simulate():
            await asyncio.sleep(0.05)
            mock_msg = MagicMock()
            mock_msg.buttons = None

            mock_event = MagicMock()
            mock_event.message = mock_msg
            mock_event.raw_text = "Login code: 12345. Do not share."
            for h in handlers:
                await h(mock_event)

        asyncio.create_task(simulate())
        result = await fragment_auth._confirm_via_telethon(client, timeout=5)
        assert result is True

    @pytest.mark.asyncio
    async def test_confirm_via_telethon_unknown_button(self, fragment_auth):
        """Test confirmation fallback: clicks first button when no keyword match."""
        client = MagicMock()
        handlers = []

        def mock_on(event_filter):
            def decorator(func):
                handlers.append(func)
                return func
            return decorator

        client.on = mock_on
        client.remove_event_handler = MagicMock()

        async def simulate():
            await asyncio.sleep(0.05)
            mock_btn = MagicMock()
            mock_btn.text = "SomeUnknownAction"
            mock_msg = MagicMock()
            mock_msg.buttons = [[mock_btn]]
            mock_msg.click = AsyncMock()

            mock_event = MagicMock()
            mock_event.message = mock_msg
            mock_event.raw_text = ""
            for h in handlers:
                await h(mock_event)

        asyncio.create_task(simulate())
        result = await fragment_auth._confirm_via_telethon(client, timeout=5)
        assert result is True

    @pytest.mark.asyncio
    async def test_confirm_via_telethon_timeout(self, fragment_auth):
        """Test confirmation timeout when no message arrives."""
        client = MagicMock()
        client.on = MagicMock(return_value=lambda f: f)
        client.remove_event_handler = MagicMock()

        result = await fragment_auth._confirm_via_telethon(client, timeout=0.1)
        assert result is False
        client.remove_event_handler.assert_called()

    # --- _wait_for_fragment_auth tests ---

    @pytest.mark.asyncio
    async def test_wait_for_fragment_auth_success(self, fragment_auth):
        """Test waiting for fragment auth - becomes authorized."""
        page = AsyncMock()
        call_count = 0

        async def mock_check(p):
            nonlocal call_count
            call_count += 1
            return "authorized" if call_count >= 2 else "not_authorized"

        with patch.object(fragment_auth, '_check_fragment_state', side_effect=mock_check):
            result = await fragment_auth._wait_for_fragment_auth(page, timeout=5)
            assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_fragment_auth_timeout(self, fragment_auth):
        """Test waiting for fragment auth - stays not_authorized, no cookies."""
        page = AsyncMock()
        mock_context = MagicMock()
        mock_context.cookies = AsyncMock(return_value=[])
        page.context = mock_context

        with patch.object(fragment_auth, '_check_fragment_state', return_value="not_authorized"):
            result = await fragment_auth._wait_for_fragment_auth(page, timeout=2)
            assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_fragment_auth_cookie_fallback(self, fragment_auth):
        """Test cookie-based auth detection when JS state is unavailable."""
        page = AsyncMock()
        mock_context = MagicMock()
        mock_context.cookies = AsyncMock(return_value=[
            {"name": "stel_ssid", "value": "abc123", "domain": "fragment.com"}
        ])
        page.context = mock_context

        with patch.object(fragment_auth, '_check_fragment_state', return_value="loading"):
            result = await fragment_auth._wait_for_fragment_auth(page, timeout=2)
            assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_fragment_auth_exception_resilient(self, fragment_auth):
        """Test that _wait_for_fragment_auth handles exceptions gracefully."""
        page = AsyncMock()
        call_count = 0

        async def mock_check(p):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("page reloading")
            return "authorized"

        with patch.object(fragment_auth, '_check_fragment_state', side_effect=mock_check):
            result = await fragment_auth._wait_for_fragment_auth(page, timeout=5)
            assert result is True

    # --- connect() integration tests ---

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
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_me.first_name = "Test"
        mock_me.id = 12345
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

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

    @pytest.mark.asyncio
    async def test_connect_full_flow(self, fragment_auth):
        """Test full connect flow: popup → phone → confirm → auth."""
        # Mock Telethon client
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_me.first_name = "Test"
        mock_me.id = 12345
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        # Mock browser
        mock_page = AsyncMock()
        mock_popup = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state', side_effect=["not_authorized", "authorized"]
        ), patch.object(
            fragment_auth, '_open_oauth_popup', return_value=mock_popup
        ), patch.object(
            fragment_auth, '_submit_phone_on_popup', return_value=True
        ), patch.object(
            fragment_auth, '_confirm_via_telethon', return_value=True
        ), patch.object(
            fragment_auth, '_wait_for_fragment_auth', return_value=True
        ), patch.object(
            fragment_auth, '_human_delay', return_value=None
        ):
            result = await fragment_auth.connect(headless=True)
            assert result.success
            assert result.telegram_connected
            assert not result.already_authorized

    @pytest.mark.asyncio
    async def test_connect_popup_failure(self, fragment_auth):
        """Test connect when OAuth popup fails to open."""
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        mock_page = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state', return_value="not_authorized"
        ), patch.object(
            fragment_auth, '_open_oauth_popup', side_effect=Exception("Popup timeout")
        ):
            result = await fragment_auth.connect(headless=True)
            assert not result.success
            assert "popup" in result.error.lower()

    @pytest.mark.asyncio
    async def test_connect_phone_submission_failure(self, fragment_auth):
        """Test connect when phone submission fails."""
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        mock_page = AsyncMock()
        mock_popup = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state', return_value="not_authorized"
        ), patch.object(
            fragment_auth, '_open_oauth_popup', return_value=mock_popup
        ), patch.object(
            fragment_auth, '_submit_phone_on_popup', return_value=False
        ), patch.object(
            fragment_auth, '_human_delay', return_value=None
        ):
            result = await fragment_auth.connect(headless=True)
            assert not result.success
            assert "phone" in result.error.lower()

    @pytest.mark.asyncio
    async def test_connect_confirmation_timeout(self, fragment_auth):
        """Test connect when Telethon confirmation times out."""
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = "79991234567"
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        mock_page = AsyncMock()
        mock_popup = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ), patch.object(
            fragment_auth, '_check_fragment_state', return_value="not_authorized"
        ), patch.object(
            fragment_auth, '_open_oauth_popup', return_value=mock_popup
        ), patch.object(
            fragment_auth, '_submit_phone_on_popup', return_value=True
        ), patch.object(
            fragment_auth, '_confirm_via_telethon', return_value=False
        ), patch.object(
            fragment_auth, '_human_delay', return_value=None
        ):
            result = await fragment_auth.connect(headless=True)
            assert not result.success
            assert "timeout" in result.error.lower() or "confirmation" in result.error.lower()

    @pytest.mark.asyncio
    async def test_connect_no_phone(self, fragment_auth):
        """Test connect when phone is not available from session."""
        mock_client = MagicMock()
        mock_me = MagicMock()
        mock_me.phone = None
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()

        mock_page = AsyncMock()
        mock_browser_ctx = AsyncMock()
        mock_browser_ctx.new_page = AsyncMock(return_value=mock_page)
        mock_browser_ctx.close = AsyncMock()

        fragment_auth.browser_manager.get_profile = MagicMock()
        fragment_auth.browser_manager.launch = AsyncMock(return_value=mock_browser_ctx)

        with patch.object(
            fragment_auth, '_create_telethon_client', return_value=mock_client
        ):
            result = await fragment_auth.connect(headless=True)
            assert not result.success
            assert "phone" in result.error.lower()


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
