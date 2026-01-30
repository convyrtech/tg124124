"""
Tests for browser_manager module.
"""
import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser_manager import (
    BrowserProfile,
    BrowserManager,
    BrowserContext,
    parse_proxy,
)


class TestBrowserProfile:
    """Tests for BrowserProfile dataclass."""

    def test_profile_paths(self, tmp_path):
        """Test that profile paths are correctly computed."""
        profile = BrowserProfile(
            name="test_account",
            path=tmp_path / "test_account",
            proxy="socks5:host:1080:user:pass"
        )

        assert profile.browser_data_path == tmp_path / "test_account" / "browser_data"
        assert profile.storage_state_path == tmp_path / "test_account" / "storage_state.json"
        assert profile.config_path == tmp_path / "test_account" / "profile_config.json"

    def test_exists_false_when_no_dir(self, tmp_path):
        """Test exists() returns False when directory doesn't exist."""
        profile = BrowserProfile(
            name="nonexistent",
            path=tmp_path / "nonexistent",
            proxy=None
        )
        assert profile.exists() is False

    def test_exists_true_when_browser_data_exists(self, tmp_path):
        """Test exists() returns True when browser_data exists."""
        profile_path = tmp_path / "existing"
        (profile_path / "browser_data").mkdir(parents=True)

        profile = BrowserProfile(
            name="existing",
            path=profile_path,
            proxy=None
        )
        assert profile.exists() is True


class TestParseProxy:
    """Tests for parse_proxy function."""

    def test_parse_full_proxy(self):
        """Test parsing proxy with auth."""
        result = parse_proxy("socks5:proxy.com:1080:user:pass")
        assert result["server"] == "socks5://proxy.com:1080"
        assert result["username"] == "user"
        assert result["password"] == "pass"

    def test_parse_proxy_no_auth(self):
        """Test parsing proxy without auth."""
        result = parse_proxy("http:proxy.com:8080")
        assert result["server"] == "http://proxy.com:8080"
        assert "username" not in result
        assert "password" not in result

    def test_invalid_proxy_raises(self):
        """Test that invalid proxy raises ValueError."""
        with pytest.raises(ValueError):
            parse_proxy("invalid")


class TestBrowserManager:
    """Tests for BrowserManager class."""

    def test_init_creates_profiles_dir(self, tmp_path):
        """Test that init creates profiles directory."""
        profiles_dir = tmp_path / "profiles"
        manager = BrowserManager(profiles_dir=profiles_dir)
        assert profiles_dir.exists()

    def test_get_profile_new(self, tmp_path):
        """Test getting a new profile."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profile = manager.get_profile("new_account", proxy="socks5:host:1080:u:p")

        assert profile.name == "new_account"
        assert profile.proxy == "socks5:host:1080:u:p"
        assert profile.created is True  # Directory doesn't exist yet

    def test_get_profile_existing(self, tmp_path):
        """Test getting an existing profile."""
        # Create existing profile directory
        (tmp_path / "existing").mkdir()

        manager = BrowserManager(profiles_dir=tmp_path)
        profile = manager.get_profile("existing")

        assert profile.name == "existing"
        assert profile.created is False  # Directory exists

    def test_list_profiles_empty(self, tmp_path):
        """Test listing profiles when none exist."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profiles = manager.list_profiles()
        assert profiles == []

    def test_list_profiles_with_profiles(self, tmp_path):
        """Test listing existing profiles."""
        # Create profile with browser_data
        profile_path = tmp_path / "account1"
        (profile_path / "browser_data").mkdir(parents=True)

        # Create config
        config = {"name": "account1", "proxy": "socks5:h:1080:u:p"}
        with open(profile_path / "profile_config.json", 'w') as f:
            json.dump(config, f)

        manager = BrowserManager(profiles_dir=tmp_path)
        profiles = manager.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].name == "account1"
        assert profiles[0].proxy == "socks5:h:1080:u:p"

    def test_list_profiles_ignores_invalid_config(self, tmp_path):
        """Test that invalid config JSON is ignored."""
        profile_path = tmp_path / "broken"
        (profile_path / "browser_data").mkdir(parents=True)

        # Create invalid JSON
        with open(profile_path / "profile_config.json", 'w') as f:
            f.write("not valid json {{{")

        manager = BrowserManager(profiles_dir=tmp_path)
        profiles = manager.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].proxy is None  # Should be None due to parse error

    def test_save_profile_config(self, tmp_path):
        """Test saving profile config."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profile = manager.get_profile("test", proxy="socks5:h:p:u:p")

        manager._save_profile_config(profile)

        config_path = tmp_path / "test" / "profile_config.json"
        assert config_path.exists()

        with open(config_path) as f:
            config = json.load(f)

        assert config["name"] == "test"
        assert config["proxy"] == "socks5:h:p:u:p"


class TestBrowserContext:
    """Tests for BrowserContext class."""

    @pytest.fixture
    def mock_context(self, tmp_path):
        """Create a mock BrowserContext."""
        profile = BrowserProfile(
            name="test",
            path=tmp_path / "test",
            proxy=None
        )
        profile.path.mkdir(parents=True)

        browser = MagicMock()
        camoufox = AsyncMock()

        return BrowserContext(profile=profile, browser=browser, camoufox=camoufox)

    @pytest.mark.asyncio
    async def test_new_page(self, mock_context):
        """Test creating new page when no existing pages."""
        mock_page = MagicMock()
        # Mock that there are no existing pages
        mock_context.browser.pages = []
        mock_context.browser.new_page = AsyncMock(return_value=mock_page)

        page = await mock_context.new_page()

        assert page == mock_page
        assert mock_context.page == mock_page

    @pytest.mark.asyncio
    async def test_new_page_reuses_existing(self, mock_context):
        """Test that new_page reuses existing page in persistent context."""
        existing_page = MagicMock()
        # Mock that there's already an existing page
        mock_context.browser.pages = [existing_page]
        mock_context.browser.new_page = AsyncMock()

        page = await mock_context.new_page()

        assert page == existing_page
        assert mock_context.page == existing_page
        # new_page should NOT be called since we're reusing
        mock_context.browser.new_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_sets_closed_flag(self, mock_context):
        """Test that close sets _closed flag."""
        await mock_context.close()
        assert mock_context._closed is True

    @pytest.mark.asyncio
    async def test_close_idempotent(self, mock_context):
        """Test that close can be called multiple times safely."""
        await mock_context.close()
        await mock_context.close()  # Should not raise
        assert mock_context._closed is True

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_context):
        """Test async context manager protocol."""
        async with mock_context as ctx:
            assert ctx is mock_context
        assert mock_context._closed is True
