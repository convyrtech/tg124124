"""Tests for GUI pre-flight proxy checks and auto-assign logic.

Tests _preflight_check_proxies(), _quick_auto_assign(), canary warning,
and _migrate_single no-proxy warning.
"""

import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock DearPyGui before any gui imports (may not be installed in test env)
_dpg_mock = MagicMock()
sys.modules.setdefault("dearpygui", _dpg_mock)
sys.modules.setdefault("dearpygui.dearpygui", _dpg_mock)

from src.database import AccountRecord, ProxyRecord  # noqa: E402

# Safe to import after dpg mock
from src.gui.app import TGWebAuthApp  # noqa: E402


# ─── Helpers ────────────────────────────────────────────────────────────────

_NOW = datetime.now()


def _account(id: int, name: str, proxy_id: int | None = None, status: str = "pending") -> AccountRecord:
    return AccountRecord(
        id=id, name=name, phone=None, username=None,
        session_path=f"accounts/{name}/session.session",
        proxy_id=proxy_id, status=status, last_check=None,
        error_message=None, created_at=_NOW,
    )


def _proxy(id: int, host: str = "1.2.3.4", port: int = 1080,
           status: str = "active", assigned_account_id: int | None = None) -> ProxyRecord:
    return ProxyRecord(
        id=id, host=host, port=port, username="user", password="pass",
        protocol="socks5", status=status,
        assigned_account_id=assigned_account_id,
        last_check=None, created_at=_NOW,
    )


def _make_app(accounts: list[AccountRecord],
              proxies: list[ProxyRecord],
              free_proxies: list[ProxyRecord] | None = None) -> TGWebAuthApp:
    """Create a minimal TGWebAuthApp with mocked DB for testing."""
    app = object.__new__(TGWebAuthApp)

    db_mock = AsyncMock()
    db_mock.list_accounts = AsyncMock(return_value=accounts)
    db_mock.list_proxies = AsyncMock(return_value=proxies)

    if free_proxies:
        # Return each free proxy once, then None (exhausted)
        db_mock.get_free_proxy = AsyncMock(side_effect=list(free_proxies) + [None])
    else:
        db_mock.get_free_proxy = AsyncMock(return_value=None)

    db_mock.assign_proxy = AsyncMock()

    controller = MagicMock()
    controller.db = db_mock
    app._controller = controller

    # Capture log messages for assertions
    app._log_messages: list[str] = []
    app._log = lambda msg: app._log_messages.append(msg)

    return app


# ─── _preflight_check_proxies ──────────────────────────────────────────────


class TestPreflightCheckProxies:
    """Test pre-flight proxy validation before batch migration."""

    @pytest.mark.asyncio
    async def test_all_accounts_have_live_proxies_returns_true(self):
        accounts = [_account(1, "acc1", proxy_id=10), _account(2, "acc2", proxy_id=20)]
        proxies = [_proxy(10, status="active"), _proxy(20, status="active")]
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1, 2])

        assert result is True
        assert not any("ОСТАНОВЛЕНО" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_account_without_proxy_returns_false(self):
        accounts = [_account(1, "acc1", proxy_id=10), _account(2, "no_proxy_acc", proxy_id=None)]
        proxies = [_proxy(10, status="active")]
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1, 2])

        assert result is False
        assert any("1 аккаунтов без прокси" in m for m in app._log_messages)
        assert any("no_proxy_acc" in m for m in app._log_messages)
        assert any("Auto-Assign" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_account_with_dead_proxy_returns_false(self):
        accounts = [_account(1, "acc1", proxy_id=10)]
        proxies = [_proxy(10, status="dead")]
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1])

        assert result is False
        assert any("мёртвым прокси" in m for m in app._log_messages)
        assert any("acc1" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_missing_proxy_record_treated_as_no_proxy(self):
        # proxy_id=99 but no ProxyRecord with id=99 in DB
        accounts = [_account(1, "orphan_acc", proxy_id=99)]
        proxies = []  # No proxies in DB at all
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1])

        assert result is False
        assert any("без прокси" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_mixed_no_proxy_and_dead_proxy(self):
        accounts = [
            _account(1, "no_proxy", proxy_id=None),
            _account(2, "dead_proxy", proxy_id=20),
            _account(3, "ok_acc", proxy_id=30),
        ]
        proxies = [_proxy(20, status="dead"), _proxy(30, status="active")]
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1, 2, 3])

        assert result is False
        assert any("без прокси" in m for m in app._log_messages)
        assert any("мёртвым прокси" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_mode_prefix_in_log(self):
        accounts = [_account(1, "acc1", proxy_id=None)]
        proxies = []
        app = _make_app(accounts, proxies)

        await app._preflight_check_proxies([1], mode="Fragment")

        assert any("[Fragment]" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_only_batch_accounts_checked(self):
        # acc3 has no proxy but is NOT in batch — should not block
        accounts = [
            _account(1, "acc1", proxy_id=10),
            _account(2, "acc2", proxy_id=20),
            _account(3, "acc3_not_in_batch", proxy_id=None),
        ]
        proxies = [_proxy(10, status="active"), _proxy(20, status="active")]
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies([1, 2])  # Only 1 and 2

        assert result is True

    @pytest.mark.asyncio
    async def test_truncates_long_name_list(self):
        # 8 accounts without proxy — should show first 5 + "и ещё 3"
        accounts = [_account(i, f"acc{i}", proxy_id=None) for i in range(1, 9)]
        proxies = []
        app = _make_app(accounts, proxies)

        result = await app._preflight_check_proxies(list(range(1, 9)))

        assert result is False
        assert any("и ещё 3" in m for m in app._log_messages)


# ─── _quick_auto_assign ────────────────────────────────────────────────────


class TestQuickAutoAssign:
    """Test quick auto-assign of free proxies before batch."""

    @pytest.mark.asyncio
    async def test_no_accounts_need_proxies(self):
        accounts = [_account(1, "acc1", proxy_id=10)]
        proxies = [_proxy(10, status="active")]
        app = _make_app(accounts, proxies)

        result = await app._quick_auto_assign([1])

        assert result == 0

    @pytest.mark.asyncio
    async def test_assigns_free_proxies(self):
        accounts = [_account(1, "acc1", proxy_id=None), _account(2, "acc2", proxy_id=None)]
        proxies = []
        free = [_proxy(50, host="5.5.5.5"), _proxy(60, host="6.6.6.6")]
        app = _make_app(accounts, proxies, free_proxies=free)

        result = await app._quick_auto_assign([1, 2])

        assert result == 2
        assert app._controller.db.assign_proxy.call_count == 2
        assert any("Авто-назначен прокси" in m for m in app._log_messages)

    @pytest.mark.asyncio
    async def test_partial_assign_when_not_enough_proxies(self):
        accounts = [
            _account(1, "acc1", proxy_id=None),
            _account(2, "acc2", proxy_id=None),
            _account(3, "acc3", proxy_id=None),
        ]
        proxies = []
        free = [_proxy(50, host="5.5.5.5")]  # Only 1 free proxy
        app = _make_app(accounts, proxies, free_proxies=free)

        result = await app._quick_auto_assign([1, 2, 3])

        assert result == 1  # Only 1 assigned, then break

    @pytest.mark.asyncio
    async def test_skips_accounts_with_existing_proxy(self):
        accounts = [
            _account(1, "has_proxy", proxy_id=10),
            _account(2, "needs_proxy", proxy_id=None),
        ]
        proxies = [_proxy(10, status="active")]
        free = [_proxy(50, host="5.5.5.5")]
        app = _make_app(accounts, proxies, free_proxies=free)

        result = await app._quick_auto_assign([1, 2])

        assert result == 1  # Only acc2 assigned
        # Verify assign_proxy was called with acc2's id
        app._controller.db.assign_proxy.assert_called_once_with(2, 50)

    @pytest.mark.asyncio
    async def test_handles_assign_value_error(self):
        accounts = [_account(1, "acc1", proxy_id=None), _account(2, "acc2", proxy_id=None)]
        proxies = []
        free = [_proxy(50), _proxy(60)]
        app = _make_app(accounts, proxies, free_proxies=free)

        # First assign raises ValueError (race), second succeeds
        app._controller.db.assign_proxy = AsyncMock(side_effect=[ValueError("conflict"), None])

        result = await app._quick_auto_assign([1, 2])

        assert result == 1  # Only second one succeeded

    @pytest.mark.asyncio
    async def test_only_processes_batch_accounts(self):
        accounts = [
            _account(1, "in_batch", proxy_id=None),
            _account(2, "not_in_batch", proxy_id=None),
        ]
        proxies = []
        free = [_proxy(50)]
        app = _make_app(accounts, proxies, free_proxies=free)

        result = await app._quick_auto_assign([1])  # Only account 1

        assert result == 1
        app._controller.db.assign_proxy.assert_called_once_with(1, 50)
