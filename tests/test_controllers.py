"""Tests for GUI controllers (import_sessions)."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database import AccountRecord
from src.gui.controllers import AppController


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def tmp_sessions_dir(tmp_path):
    """Create a temporary sessions directory."""
    sessions_dir = tmp_path / "accounts"
    sessions_dir.mkdir()
    return sessions_dir


@pytest.fixture
def tmp_source_dir(tmp_path):
    """Create a temporary source directory with session files."""
    source = tmp_path / "source"
    source.mkdir()
    return source


def _create_session_file(directory: Path, name: str, size: int = 2048,
                         proxy: str | None = None, config_name: str | None = None):
    """Helper to create a fake session file with optional config."""
    account_dir = directory / name
    account_dir.mkdir(exist_ok=True)

    # Create session file with fake content
    session_file = account_dir / "session.session"
    session_file.write_bytes(b"\x00" * size)

    # Create api.json
    api_json = account_dir / "api.json"
    api_json.write_text(json.dumps({"api_id": 12345, "api_hash": "abc"}))

    # Create config if proxy or name provided
    if proxy or config_name:
        config = {}
        if proxy:
            config["Proxy"] = proxy
        if config_name:
            config["Name"] = config_name
        cfg_file = account_dir / "___config.json"
        cfg_file.write_text(json.dumps(config))

    return session_file


@pytest.fixture
def controller(tmp_data_dir, tmp_sessions_dir):
    """Create an AppController with mocked database."""
    ctrl = AppController(tmp_data_dir)
    ctrl.sessions_dir = tmp_sessions_dir

    # Mock database
    ctrl.db = AsyncMock()
    ctrl.db.list_accounts = AsyncMock(return_value=[])
    ctrl.db.add_account = AsyncMock(side_effect=lambda **kw: (1, True))  # return (account_id, created)
    ctrl.db.assign_proxy = AsyncMock()
    ctrl.db.find_proxy_by_host_port = AsyncMock(return_value=None)
    ctrl.db.add_proxy = AsyncMock(return_value=1)

    return ctrl


class TestImportSessions:
    """Tests for AppController.import_sessions."""

    @pytest.mark.asyncio
    async def test_import_basic(self, controller, tmp_source_dir):
        """Import a single valid session file."""
        _create_session_file(tmp_source_dir, "account1")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 1
        assert skipped == 0
        controller.db.add_account.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_empty_dir(self, controller, tmp_source_dir):
        """Import from empty directory returns (0, 0)."""
        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 0
        assert skipped == 0

    @pytest.mark.asyncio
    async def test_import_skips_existing(self, controller, tmp_source_dir):
        """Skip accounts that already exist in DB."""
        _create_session_file(tmp_source_dir, "existing_account")

        controller.db.list_accounts.return_value = [
            AccountRecord(id=1, name="existing_account", phone=None, username=None,
                          session_path="accounts/existing_account/session.session",
                          proxy_id=None, status="healthy", last_check=None,
                          error_message=None, created_at="2026-01-01",
                          fragment_status=None, web_last_verified=None, auth_ttl_days=None)
        ]

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 0
        assert skipped == 1
        controller.db.add_account.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_skips_small_files(self, controller, tmp_source_dir):
        """Skip session files that are too small (<1024 bytes)."""
        _create_session_file(tmp_source_dir, "tiny_account", size=100)

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 0
        assert skipped == 1

    @pytest.mark.asyncio
    async def test_import_with_proxy(self, controller, tmp_source_dir):
        """Import account with proxy config assigns proxy."""
        _create_session_file(tmp_source_dir, "acc_with_proxy",
                             proxy="socks5:1.2.3.4:1080:user:pass")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 1
        assert skipped == 0
        controller.db.assign_proxy.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_proxy_error_does_not_block(self, controller, tmp_source_dir):
        """BUG FIX: Proxy assignment error should NOT prevent account from being imported."""
        _create_session_file(tmp_source_dir, "acc1",
                             proxy="socks5:1.2.3.4:1080:user:pass")

        # Make assign_proxy raise ValueError (e.g., proxy already assigned)
        controller.db.assign_proxy.side_effect = ValueError("Proxy 1 already assigned to account 99")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        # Account should still be counted as imported
        assert imported == 1
        assert skipped == 0
        controller.db.add_account.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_multiple_accounts_same_proxy(self, controller, tmp_source_dir):
        """Multiple accounts with same proxy: all import, only first gets proxy."""
        account_id_counter = [0]

        async def _add_account(**kw):
            account_id_counter[0] += 1
            return (account_id_counter[0], True)

        controller.db.add_account.side_effect = _add_account

        # First assign_proxy succeeds, rest fail
        call_count = [0]
        async def _assign_proxy(account_id, proxy_id):
            call_count[0] += 1
            if call_count[0] > 1:
                raise ValueError(f"Proxy {proxy_id} already assigned to account 1")

        controller.db.assign_proxy.side_effect = _assign_proxy

        for i in range(3):
            _create_session_file(tmp_source_dir, f"acc_{i}",
                                 proxy="socks5:1.2.3.4:1080:user:pass")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        # All 3 should be imported despite proxy errors
        assert imported == 3
        assert skipped == 0

    @pytest.mark.asyncio
    async def test_import_self_copy_same_dir(self, controller, tmp_sessions_dir):
        """BUG FIX: Import from same directory as destination should work (samefile)."""
        # Create session directly in the sessions dir (simulating self-import)
        _create_session_file(tmp_sessions_dir, "self_account")

        imported, skipped = await controller.import_sessions(tmp_sessions_dir)

        assert imported == 1
        assert skipped == 0
        controller.db.add_account.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_with_config_name(self, controller, tmp_source_dir):
        """Import account with config name builds composite name."""
        _create_session_file(tmp_source_dir, "12345678",
                             config_name="My Account")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 1
        # Check the name passed to add_account
        call_kwargs = controller.db.add_account.call_args[1]
        assert call_kwargs["name"] == "12345678 (My Account)"

    @pytest.mark.asyncio
    async def test_import_progress_callback(self, controller, tmp_source_dir):
        """Progress callback is called for each session."""
        _create_session_file(tmp_source_dir, "acc1")
        _create_session_file(tmp_source_dir, "acc2")

        progress_calls = []

        def on_progress(current, total, msg):
            progress_calls.append((current, total, msg))

        account_id_counter = [0]
        async def _add_account(**kw):
            account_id_counter[0] += 1
            return (account_id_counter[0], True)
        controller.db.add_account.side_effect = _add_account

        imported, skipped = await controller.import_sessions(tmp_source_dir, on_progress=on_progress)

        assert imported == 2
        assert len(progress_calls) == 2
        # All progress calls should have total=2
        assert all(t == 2 for _, t, _ in progress_calls)

    @pytest.mark.asyncio
    async def test_import_corrects_existing_proxy_protocol(self, controller, tmp_source_dir):
        """When existing proxy has wrong protocol (socks5 on port 80), it gets corrected to http."""
        from src.database import ProxyRecord

        # Proxy already in DB as socks5 (wrong)
        controller.db.find_proxy_by_host_port = AsyncMock(return_value=99)
        existing_proxy = ProxyRecord(
            id=99, host="p.webshare.io", port=80,
            username="user", password="pass",
            protocol="socks5",  # wrong — should be http
            status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        controller.db.get_proxy = AsyncMock(return_value=existing_proxy)
        controller.db.update_proxy = AsyncMock()

        _create_session_file(tmp_source_dir, "acc1",
                             proxy="socks5:p.webshare.io:80:user:pass")
        await controller.import_sessions(tmp_source_dir)

        # update_proxy should be called to fix the protocol
        controller.db.update_proxy.assert_called_once_with(99, protocol="http")

    @pytest.mark.asyncio
    async def test_import_correct_protocol_not_updated(self, controller, tmp_source_dir):
        """When existing proxy already has correct protocol, update_proxy is NOT called."""
        from src.database import ProxyRecord

        controller.db.find_proxy_by_host_port = AsyncMock(return_value=5)
        existing_proxy = ProxyRecord(
            id=5, host="1.2.3.4", port=1080,
            username="user", password="pass",
            protocol="socks5",  # correct
            status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        controller.db.get_proxy = AsyncMock(return_value=existing_proxy)
        controller.db.update_proxy = AsyncMock()

        _create_session_file(tmp_source_dir, "acc1",
                             proxy="socks5:1.2.3.4:1080:user:pass")
        await controller.import_sessions(tmp_source_dir)

        controller.db.update_proxy.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_corrupt_config_still_imports(self, controller, tmp_source_dir):
        """Corrupt config JSON should not prevent import — just skip proxy."""
        account_dir = tmp_source_dir / "corrupt_cfg"
        account_dir.mkdir()
        (account_dir / "session.session").write_bytes(b"\x00" * 2048)
        (account_dir / "___config.json").write_text("NOT VALID JSON {{{")

        imported, skipped = await controller.import_sessions(tmp_source_dir)

        assert imported == 1
        assert skipped == 0
        controller.db.assign_proxy.assert_not_called()
