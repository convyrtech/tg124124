"""
Tests for browser_manager module.
"""
import asyncio
import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.browser_manager import (
    BrowserProfile,
    BrowserManager,
    BrowserContext,
    ProfileLifecycleManager,
    _get_browser_pid,
    _get_driver_pid,
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
        """Test saving profile config strips credentials from proxy."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profile = manager.get_profile("test", proxy="socks5:h:p:u:p")

        manager._save_profile_config(profile)

        config_path = tmp_path / "test" / "profile_config.json"
        assert config_path.exists()

        with open(config_path) as f:
            config = json.load(f)

        assert config["name"] == "test"
        # Credentials must be stripped — only protocol:host:port stored
        assert config["proxy"] == "socks5:h:p"
        assert "u" not in config["proxy"]


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
    async def test_close_skips_storage_save_by_default(self, mock_context):
        """FIX #6: close() should NOT save storage_state when save_state_on_close=False."""
        mock_context.save_storage_state = AsyncMock()
        await mock_context.close()
        mock_context.save_storage_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_saves_storage_when_flagged(self, mock_context):
        """FIX #6: close() should save storage_state when save_state_on_close=True."""
        mock_context.save_state_on_close = True
        mock_context.save_storage_state = AsyncMock()
        await mock_context.close()
        mock_context.save_storage_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_context):
        """Test async context manager protocol."""
        async with mock_context as ctx:
            assert ctx is mock_context
        assert mock_context._closed is True


def _make_hot_profile(profiles_dir: Path, name: str, files: dict[str, bytes] | None = None) -> Path:
    """Helper: create a hot profile directory with browser_data/ and optional files."""
    profile_path = profiles_dir / name
    (profile_path / "browser_data").mkdir(parents=True, exist_ok=True)
    if files:
        for rel_path, content in files.items():
            file_path = profile_path / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
    return profile_path


class TestProfileLifecycleManager:
    """Tests for ProfileLifecycleManager hot/cold tiering."""

    @pytest.mark.asyncio
    async def test_ensure_active_new_profile(self, tmp_path):
        """ensure_active on non-existent profile returns path without crash."""
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)
        result = await mgr.ensure_active("brand_new")

        assert result == tmp_path / "brand_new"
        assert "brand_new" in mgr._access_order

    @pytest.mark.asyncio
    async def test_hibernate_creates_zip_removes_dir(self, tmp_path):
        """hibernate compresses profile to zip and removes directory."""
        _make_hot_profile(tmp_path, "acct1", {"browser_data/data.txt": b"hello"})
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)

        assert mgr.is_hot("acct1")
        zip_path = await mgr.hibernate("acct1")

        assert zip_path == tmp_path / "acct1.zip"
        assert zip_path.exists()
        assert not (tmp_path / "acct1").exists()
        assert "acct1" not in mgr._access_order
        assert mgr.is_cold("acct1")

    @pytest.mark.asyncio
    async def test_ensure_active_decompresses_cold(self, tmp_path):
        """ensure_active on cold profile decompresses and deletes zip."""
        _make_hot_profile(tmp_path, "acct2", {"browser_data/test.db": b"data"})
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)
        await mgr.hibernate("acct2")

        assert mgr.is_cold("acct2")
        result = await mgr.ensure_active("acct2")

        assert result == tmp_path / "acct2"
        assert mgr.is_hot("acct2")
        assert not (tmp_path / "acct2.zip").exists()
        assert "acct2" in mgr._access_order

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_files(self, tmp_path):
        """hibernate then ensure_active preserves all file contents."""
        files = {
            "browser_data/test.txt": b"hello world",
            "browser_data/subdir/nested.bin": b"\x00\x01\x02\xff",
            "profile_config.json": b'{"name": "rt_test"}',
        }
        _make_hot_profile(tmp_path, "roundtrip", files)
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)

        await mgr.hibernate("roundtrip")
        await mgr.ensure_active("roundtrip")

        for rel_path, expected in files.items():
            actual = (tmp_path / "roundtrip" / rel_path).read_bytes()
            assert actual == expected, f"File {rel_path} content mismatch"

    @pytest.mark.asyncio
    async def test_lru_eviction(self, tmp_path):
        """When max_hot is reached, oldest LRU profile is evicted."""
        _make_hot_profile(tmp_path, "a")
        _make_hot_profile(tmp_path, "b")
        mgr = ProfileLifecycleManager(tmp_path, max_hot=2)

        # Touch order: a (oldest), b (newest)
        # ensure_active on "c" (new, not yet on disk) should evict "a"
        await mgr.ensure_active("c")

        assert mgr.is_cold("a"), "Oldest profile 'a' should have been evicted"
        assert mgr.is_hot("b")
        assert "c" in mgr._access_order

    @pytest.mark.asyncio
    async def test_eviction_skips_protected(self, tmp_path):
        """Eviction skips profiles in the protected set."""
        _make_hot_profile(tmp_path, "p1")
        _make_hot_profile(tmp_path, "p2")
        mgr = ProfileLifecycleManager(tmp_path, max_hot=2)

        # Protect p1 (oldest), so p2 should be evicted instead
        await mgr.ensure_active("p3", protected={"p1"})

        assert mgr.is_hot("p1"), "Protected profile 'p1' should NOT be evicted"
        assert mgr.is_cold("p2"), "Unprotected 'p2' should be evicted"
        assert "p3" in mgr._access_order

    @pytest.mark.asyncio
    async def test_hibernate_nonexistent_returns_none(self, tmp_path):
        """hibernate on non-existent profile returns None without crash."""
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)
        result = await mgr.hibernate("nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_stats(self, tmp_path):
        """get_stats returns correct hot/cold/total counts."""
        _make_hot_profile(tmp_path, "hot1")
        _make_hot_profile(tmp_path, "hot2")
        _make_hot_profile(tmp_path, "to_cold", {"browser_data/x.txt": b"x"})
        mgr = ProfileLifecycleManager(tmp_path, max_hot=10)
        await mgr.hibernate("to_cold")

        stats = mgr.get_stats()
        assert stats["hot"] == 2
        assert stats["cold"] == 1
        assert stats["total"] == 3
        assert stats["max_hot"] == 10

    @pytest.mark.asyncio
    async def test_double_ensure_noop(self, tmp_path):
        """Calling ensure_active twice on hot profile doesn't duplicate in LRU."""
        _make_hot_profile(tmp_path, "dup")
        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)

        await mgr.ensure_active("dup")
        await mgr.ensure_active("dup")

        assert mgr._access_order.count("dup") == 1

    @pytest.mark.asyncio
    async def test_rmtree_readonly_files(self, tmp_path):
        """hibernate succeeds even with read-only files (Windows PermissionError)."""
        profile_path = _make_hot_profile(tmp_path, "readonly_test", {
            "browser_data/locked.db": b"locked data",
        })
        # Make file read-only
        locked_file = profile_path / "browser_data" / "locked.db"
        locked_file.chmod(0o444)

        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)
        zip_path = await mgr.hibernate("readonly_test")

        assert zip_path is not None
        assert zip_path.exists()
        assert not profile_path.exists()

    def test_sync_access_order_on_init(self, tmp_path):
        """Init rebuilds _access_order from filesystem modification times."""
        import time

        _make_hot_profile(tmp_path, "older")
        time.sleep(0.05)  # Ensure different mtime
        _make_hot_profile(tmp_path, "newer")

        mgr = ProfileLifecycleManager(tmp_path, max_hot=5)

        assert "older" in mgr._access_order
        assert "newer" in mgr._access_order
        # older should come before newer in LRU order
        assert mgr._access_order.index("older") < mgr._access_order.index("newer")


class TestGetBrowserPid:
    """Tests for _get_browser_pid() function."""

    def test_returns_none_on_missing_connection(self):
        """Should return None when camoufox has no _connection."""
        fake = MagicMock(spec=[])  # no attributes
        assert _get_browser_pid(fake) is None

    def test_returns_none_on_attribute_error(self):
        """Should return None when transport chain is broken."""
        fake = MagicMock()
        del fake._connection  # force AttributeError
        assert _get_browser_pid(fake) is None

    @patch("src.browser_manager.psutil.Process")
    def test_returns_child_pid(self, mock_process_cls):
        """Should return child PID matching camoufox/firefox name."""
        # Setup fake camoufox with PID chain
        fake = MagicMock()
        fake._connection._transport._proc.pid = 1234

        child = MagicMock()
        child.name.return_value = "camoufox.exe"
        child.pid = 5678

        mock_parent = MagicMock()
        mock_parent.children.return_value = [child]
        mock_process_cls.return_value = mock_parent

        assert _get_browser_pid(fake) == 5678

    @patch("src.browser_manager.psutil.Process")
    def test_returns_node_pid_as_fallback(self, mock_process_cls):
        """Should return node_pid when no camoufox child found."""
        fake = MagicMock()
        fake._connection._transport._proc.pid = 1234

        # No matching children
        other_child = MagicMock()
        other_child.name.return_value = "some_other.exe"

        mock_parent = MagicMock()
        mock_parent.children.return_value = [other_child]
        mock_process_cls.return_value = mock_parent

        assert _get_browser_pid(fake) == 1234

    @patch("src.browser_manager.psutil.Process")
    def test_handles_no_such_process(self, mock_process_cls):
        """Should return None when process doesn't exist."""
        import psutil
        fake = MagicMock()
        fake._connection._transport._proc.pid = 9999
        mock_process_cls.side_effect = psutil.NoSuchProcess(9999)

        assert _get_browser_pid(fake) is None


class TestGetDriverPid:
    """Tests for _get_driver_pid() function."""

    def test_returns_driver_pid(self):
        """Should return the transport process PID (Playwright driver)."""
        fake = MagicMock()
        fake._connection._transport._proc.pid = 1234
        assert _get_driver_pid(fake) == 1234

    def test_returns_none_on_missing_connection(self):
        """Should return None when camoufox has no _connection."""
        fake = MagicMock(spec=[])
        assert _get_driver_pid(fake) is None

    def test_returns_none_on_attribute_error(self):
        """Should return None when transport chain is broken."""
        fake = MagicMock()
        del fake._connection
        assert _get_driver_pid(fake) is None


class TestBrowserContextDriverPid:
    """Tests for _driver_pid field on BrowserContext."""

    def test_driver_pid_initialized_none(self):
        """BrowserContext._driver_pid defaults to None."""
        profile = BrowserProfile(name="test", path=Path("/tmp/test"), proxy=None)
        ctx = BrowserContext(
            profile=profile,
            browser=MagicMock(),
            camoufox=MagicMock(),
        )
        assert ctx._driver_pid is None

    def test_driver_pid_can_be_set(self):
        """BrowserContext._driver_pid can be assigned after init."""
        profile = BrowserProfile(name="test", path=Path("/tmp/test"), proxy=None)
        ctx = BrowserContext(
            profile=profile,
            browser=MagicMock(),
            camoufox=MagicMock(),
        )
        ctx._driver_pid = 5678
        assert ctx._driver_pid == 5678


class TestBrowserContextForceKill:
    """Tests for BrowserContext._force_kill_by_pid()."""

    def _make_ctx(self, browser_pid=None):
        """Create a BrowserContext with mocked internals."""
        profile = BrowserProfile(name="test", path=Path("/tmp/test"), proxy=None)
        ctx = BrowserContext(
            profile=profile,
            browser=MagicMock(),
            camoufox=MagicMock(),
        )
        ctx._browser_pid = browser_pid
        return ctx

    def test_noop_when_no_pid(self):
        """Should do nothing when _browser_pid is None."""
        ctx = self._make_ctx(browser_pid=None)
        ctx._force_kill_by_pid()  # should not raise

    @patch("src.browser_manager.psutil.Process")
    def test_kills_process_and_children(self, mock_process_cls):
        """Should kill children first, then main process."""
        child = MagicMock()
        proc = MagicMock()
        proc.children.return_value = [child]
        mock_process_cls.return_value = proc

        ctx = self._make_ctx(browser_pid=42)
        ctx._force_kill_by_pid()

        child.kill.assert_called_once()
        proc.kill.assert_called_once()

    @patch("src.browser_manager.psutil.Process")
    def test_handles_already_gone(self, mock_process_cls):
        """Should handle NoSuchProcess gracefully."""
        import psutil
        mock_process_cls.side_effect = psutil.NoSuchProcess(42)

        ctx = self._make_ctx(browser_pid=42)
        ctx._force_kill_by_pid()  # should not raise


class TestLaunchRelayRecreation:
    """Tests that proxy relay is recreated on retry after timeout."""

    @pytest.mark.asyncio
    async def test_relay_recreated_on_retry(self, tmp_path):
        """On TimeoutError retry, old relay should be stopped and new one created."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profile = BrowserProfile(
            name="test_relay",
            path=tmp_path / "test_relay",
            proxy="socks5:host:1080:user:pass",
        )

        # Track ProxyRelay instances
        relay_instances = []

        class FakeRelay:
            def __init__(self, proxy_str):
                self.proxy_str = proxy_str
                self.started = False
                self.stopped = False
                self.local_host = "127.0.0.1"
                self.local_port = 9999 + len(relay_instances)
                self.local_url = f"http://127.0.0.1:{self.local_port}"
                self.browser_proxy_config = {"server": self.local_url}
                relay_instances.append(self)

            async def start(self):
                self.started = True

            async def stop(self):
                self.stopped = True

        mock_browser = MagicMock()
        call_count = 0

        async def fake_aenter(self_cm):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            return mock_browser

        with patch("src.browser_manager.ProxyRelay", FakeRelay), \
             patch("src.browser_manager.AsyncCamoufox") as MockCamoufox, \
             patch("src.browser_manager._get_browser_pid", return_value=123), \
             patch("src.browser_manager._get_driver_pid", return_value=100), \
             patch.object(manager, "_kill_zombie_browser", new_callable=AsyncMock):

            mock_instance = MagicMock()
            mock_instance.__aenter__ = fake_aenter
            MockCamoufox.return_value = mock_instance

            ctx = await manager.launch(profile, headless=True)

            # Two relay instances: original + recreated on retry
            assert len(relay_instances) == 2
            # First relay was stopped
            assert relay_instances[0].stopped is True
            # Second relay was started
            assert relay_instances[1].started is True
            # Context uses the new relay
            assert ctx._proxy_relay is relay_instances[1]

    @pytest.mark.asyncio
    async def test_relay_stop_error_on_retry_is_handled(self, tmp_path):
        """If old relay.stop() raises, new relay is still created."""
        manager = BrowserManager(profiles_dir=tmp_path)
        profile = BrowserProfile(
            name="test_relay_err",
            path=tmp_path / "test_relay_err",
            proxy="socks5:host:1080:user:pass",
        )

        relay_instances = []

        class FakeRelay:
            def __init__(self, proxy_str):
                self.started = False
                self.stopped = False
                self.local_host = "127.0.0.1"
                self.local_port = 8888 + len(relay_instances)
                self.local_url = f"http://127.0.0.1:{self.local_port}"
                self.browser_proxy_config = {"server": self.local_url}
                relay_instances.append(self)

            async def start(self):
                self.started = True

            async def stop(self):
                if not self.stopped:
                    self.stopped = True
                    if len(relay_instances) == 1:
                        raise OSError("relay stop failed")

        mock_browser = MagicMock()
        call_count = 0

        async def fake_aenter(self_cm):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            return mock_browser

        with patch("src.browser_manager.ProxyRelay", FakeRelay), \
             patch("src.browser_manager.AsyncCamoufox") as MockCamoufox, \
             patch("src.browser_manager._get_browser_pid", return_value=456), \
             patch("src.browser_manager._get_driver_pid", return_value=400), \
             patch.object(manager, "_kill_zombie_browser", new_callable=AsyncMock):

            mock_instance = MagicMock()
            mock_instance.__aenter__ = fake_aenter
            MockCamoufox.return_value = mock_instance

            ctx = await manager.launch(profile, headless=True)

            # Despite stop() error, new relay was created
            assert len(relay_instances) == 2
            assert relay_instances[1].started is True
            assert ctx._proxy_relay is relay_instances[1]
