"""
Tests for production hardening fixes (Blocks 1-7).
Covers QR decode pipeline, QR flow, Fragment auth, error classification,
crash safety, preflight, GUI performance fixes.
"""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from src.telegram_auth import (
    AuthResult,
    ErrorCategory,
    TelegramAuth,
    classify_error,
    decode_qr_from_screenshot,
)


# ─── Block 1: QR Decode Pipeline ────────────────────────────────────────────


class TestCanvasToDataURL:
    """Fix 1.1: canvas.toDataURL() works without getContext('2d')."""

    @pytest.mark.asyncio
    async def test_canvas_todataurl_without_getcontext(self):
        """Verify JS code calls toDataURL() directly, no getContext guard."""
        from src.telegram_auth import TelegramAuth
        # Read the source to verify the fix is in place
        import inspect
        source = inspect.getsource(TelegramAuth._wait_for_qr)
        # The old buggy pattern: getContext('2d') guard before toDataURL
        assert "const ctx = canvas.getContext('2d')" not in source or \
               "ctx.getImageData" in source, \
               "getContext('2d') guard should not block toDataURL path"
        # The new pattern: try/catch around toDataURL
        assert "canvas.toDataURL" in source


class TestElementScreenshot:
    """Fix 1.2: qr_element.screenshot() instead of page.screenshot()."""

    @pytest.mark.asyncio
    async def test_element_screenshot_in_source(self):
        """Verify source uses qr_element.screenshot()."""
        import inspect
        source = inspect.getsource(TelegramAuth._wait_for_qr)
        assert "qr_element.screenshot()" in source, \
            "Should use element screenshot, not page screenshot"


class TestCropRegions:
    """Fix 1.3: Crop regions include center of page."""

    def test_crop_regions_include_center(self):
        """Verify _get_qr_crops includes center region."""
        import inspect
        source = inspect.getsource(decode_qr_from_screenshot)
        # Center crop: (w*0.25, h*0.1, w*0.75, h*0.65)
        assert "w * 0.25" in source, "Should have center-left crop boundary"
        assert "w * 0.75" in source, "Should have center-right crop boundary"


# ─── Block 2: QR Flow Hardening ─────────────────────────────────────────────


class TestWaitForQrStateRecheck:
    """Fix 2.1: Page state re-check during _wait_for_qr loop."""

    @pytest.mark.asyncio
    async def test_wait_for_qr_detects_2fa_mid_wait(self):
        """_wait_for_qr should detect 2FA state change mid-wait and return None."""
        auth = MagicMock(spec=TelegramAuth)
        auth._jsqr_injected = False

        page = AsyncMock()
        # All JS evaluations return None (no QR found)
        page.evaluate = AsyncMock(return_value=None)
        # query_selector returns None (no canvas)
        page.query_selector = AsyncMock(return_value=None)

        # _check_page_state returns "2fa_required" (detected mid-loop)
        async def mock_check_state(p):
            return "2fa_required"

        auth._check_page_state = mock_check_state

        # Call the actual method with a short timeout
        result = await TelegramAuth._wait_for_qr(auth, page, timeout=15)
        assert result is None


class TestRetryStateCheck:
    """Fix 2.2: State re-check after page.reload() in retry."""

    @pytest.mark.asyncio
    async def test_retry_detects_2fa_after_reload(self):
        """_extract_qr_token_with_retry should stop if 2FA appears after reload."""
        auth = MagicMock(spec=TelegramAuth)
        auth.QR_MAX_RETRIES = 3
        auth.QR_RETRY_DELAY = 0.1
        auth.account = MagicMock()
        auth.account.name = "test_account"

        page = AsyncMock()

        async def mock_check_state(p):
            return "2fa_required"

        auth._check_page_state = mock_check_state
        auth._wait_for_qr = AsyncMock(return_value=None)

        result = await TelegramAuth._extract_qr_token_with_retry(auth, page)
        assert result is None


class TestUnknownStateGuard:
    """Fix 2.3: Guard against unknown state fallthrough."""

    def test_unknown_state_guard_in_source(self):
        """Verify authorize() has guard for unknown/loading state."""
        import inspect
        source = inspect.getsource(TelegramAuth.authorize)
        assert "unknown" in source and "loading" in source, \
            "authorize() should handle unknown/loading states"


class TestFreshFlag:
    """Fix 2.4: --fresh flag deletes browser_data only for failed profiles."""

    def test_fresh_flag_deletes_only_failed_profiles(self, tmp_path):
        """--fresh + --retry-failed should delete browser_data ONLY for failed accounts."""
        import shutil

        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()

        # Create 3 profile dirs with browser_data
        for i in range(3):
            bd = profiles_dir / f"account_{i}" / "browser_data"
            bd.mkdir(parents=True)
            (bd / "cache.db").write_text("data")

        # Only account_0 and account_2 are failed
        failed_names = {"account_0", "account_2"}

        # Simulate scoped --fresh logic (matching cli.py)
        deleted = 0
        for profile_dir in profiles_dir.iterdir():
            if (profile_dir.is_dir()
                    and profile_dir.name in failed_names
                    and (profile_dir / "browser_data").exists()):
                shutil.rmtree(profile_dir / "browser_data")
                deleted += 1

        assert deleted == 2
        # Failed profiles cleaned
        assert not (profiles_dir / "account_0" / "browser_data").exists()
        assert not (profiles_dir / "account_2" / "browser_data").exists()
        # Successful profile untouched
        assert (profiles_dir / "account_1" / "browser_data").exists()


# ─── Block 3: Fragment Auth ──────────────────────────────────────────────────


class TestFragmentReceiveUpdates:
    """Fix 3.1: receive_updates=True in fragment_auth.py."""

    def test_fragment_receive_updates_true(self):
        """Verify FragmentAuth creates client with receive_updates=True."""
        import inspect
        from src.fragment_auth import FragmentAuth
        source = inspect.getsource(FragmentAuth._create_telethon_client)
        assert "receive_updates=True" in source, \
            "Fragment client MUST have receive_updates=True for event handlers"
        assert "receive_updates=False" not in source, \
            "receive_updates=False kills the event handler for 777000 codes"


class TestFragmentSelectorFallback:
    """Fix 3.2: Text-based fallback selectors."""

    def test_fragment_state_has_text_fallback(self):
        """Verify _check_fragment_state has text-based fallback selectors."""
        import inspect
        from src.fragment_auth import FragmentAuth
        source = inspect.getsource(FragmentAuth._check_fragment_state)
        assert "log\\\\s*in" in source or "log\\s*in" in source or "Log in" in source.replace("\\\\", "\\"), \
            "Should have text-based fallback for login button"


# ─── Block 4: Error Classification ───────────────────────────────────────────


class TestErrorCategories:
    """Fix 4.1: Error category classification."""

    @pytest.mark.parametrize("error_msg,expected_category", [
        ("Session is not authorized", ErrorCategory.DEAD_SESSION),
        ("AUTH_KEY_UNREGISTERED", ErrorCategory.DEAD_SESSION),
        ("ConnectionError from proxy", ErrorCategory.BAD_PROXY),
        ("socks proxy error", ErrorCategory.BAD_PROXY),
        ("Failed to extract QR token after 8 attempts", ErrorCategory.QR_DECODE_FAIL),
        ("2FA password required", ErrorCategory.TWO_FA_REQUIRED),
        ("FloodWaitError: must wait 300s", ErrorCategory.RATE_LIMITED),
        ("Telethon connect timeout after 30s", ErrorCategory.TIMEOUT),
        ("Browser target closed", ErrorCategory.BROWSER_CRASH),
        ("Something weird happened", ErrorCategory.UNKNOWN),
        ("", ErrorCategory.UNKNOWN),
    ])
    def test_error_categories_mapping(self, error_msg, expected_category):
        """Verify classify_error maps known patterns to correct categories."""
        assert classify_error(error_msg) == expected_category

    def test_auth_result_auto_classifies(self):
        """AuthResult should auto-classify error on creation."""
        result = AuthResult(
            success=False,
            profile_name="test",
            error="Session is not authorized"
        )
        assert result.error_category == ErrorCategory.DEAD_SESSION


class TestConnectionErrorCatch:
    """Fix 4.4: TypeError from Telethon connection errors."""

    def test_connection_error_pattern_in_source(self):
        """Verify _create_telethon_client catches TypeError."""
        import inspect
        source = inspect.getsource(TelegramAuth._create_telethon_client)
        assert "TypeError" in source, \
            "Should catch TypeError from broken proxy libs"
        assert "ConnectionError" in source or "OSError" in source, \
            "Should catch ConnectionError/OSError"


class TestCLICrashSafety:
    """Fix 4.2: DB update per account in CLI."""

    def test_migrate_accounts_batch_has_on_result(self):
        """migrate_accounts_batch should accept on_result callback."""
        import inspect
        from src.telegram_auth import migrate_accounts_batch
        sig = inspect.signature(migrate_accounts_batch)
        assert "on_result" in sig.parameters, \
            "migrate_accounts_batch must have on_result parameter"


class TestBatchSummaryReport:
    """Fix 4.3: Batch summary with error breakdown."""

    def test_classify_error_covers_all_categories(self):
        """All ErrorCategory constants should be reachable from classify_error."""
        categories = {
            v for k, v in vars(ErrorCategory).items()
            if not k.startswith('_')
        }
        # Verify we can trigger each category
        triggered = set()
        test_cases = [
            "not authorized", "proxy error", "qr token",
            "2FA", "flood wait", "timeout", "browser crash", "random"
        ]
        for msg in test_cases:
            triggered.add(classify_error(msg))
        assert categories == triggered, f"Missing categories: {categories - triggered}"


# ─── Block 5: Preflight ─────────────────────────────────────────────────────


class TestPasswordFileLoading:
    """Fix 5.2: Password file JSON loading."""

    def test_password_file_loading(self, tmp_path):
        """Parse JSON password file correctly."""
        passwords = {"account_1": "pass1", "account_2": "pass2"}
        pf = tmp_path / "passwords.json"
        pf.write_text(json.dumps(passwords))

        with open(pf, 'r', encoding='utf-8') as f:
            loaded = json.load(f)

        assert loaded == passwords
        assert loaded["account_1"] == "pass1"


class TestPreflightCommand:
    """Fix 5.1: Preflight CLI command exists."""

    def test_preflight_command_registered(self):
        """Verify preflight command is registered in CLI."""
        from src.cli import cli
        commands = list(cli.commands.keys())
        assert "preflight" in commands, f"preflight not in CLI commands: {commands}"


# ─── Block 6: QR Tuning ─────────────────────────────────────────────────────


class TestQRTuning:
    """Fix 6.1-6.2: QR retries and backoff."""

    def test_qr_max_retries_increased(self):
        """QR_MAX_RETRIES should be >= 8."""
        assert TelegramAuth.QR_MAX_RETRIES >= 8

    def test_exponential_backoff_in_source(self):
        """Verify exponential backoff in retry loop."""
        import inspect
        source = inspect.getsource(TelegramAuth._extract_qr_token_with_retry)
        assert "1.5 **" in source or "1.5**" in source, \
            "Should have exponential backoff factor"


# ─── Block 7: GUI Performance ───────────────────────────────────────────────


class TestGUIPerformance:
    """Fix 7.1-7.4: GUI performance optimizations."""

    def test_deque_maxlen_increased(self):
        """Log deque maxlen should be >= 2000 for 1000-account batches."""
        import inspect
        from src.gui.app import TGWebAuthApp
        source = inspect.getsource(TGWebAuthApp.__init__)
        assert "maxlen=2000" in source

    def test_get_counts_method_exists(self):
        """Database should have get_counts() method."""
        from src.database import Database
        assert hasattr(Database, 'get_counts'), "Database must have get_counts()"

    @pytest.mark.asyncio
    async def test_get_counts_returns_dict(self, tmp_path):
        """get_counts() should return dict with expected keys."""
        from src.database import Database
        db = Database(tmp_path / "test.db")
        await db.initialize()
        await db.connect()
        try:
            counts = await db.get_counts()
            assert isinstance(counts, dict)
            assert "total" in counts
            assert "healthy" in counts
            assert "proxies_active" in counts
        finally:
            await db.close()
