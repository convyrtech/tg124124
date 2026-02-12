"""Tests for proxy health check batch."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from src.database import Database
from src.proxy_health import check_proxy_batch, check_proxy_telegram, ProxyCheckResult


class TestCheckProxyTelegram:
    """Tests for deep SOCKS5+Telegram connectivity check."""

    @pytest.mark.asyncio
    async def test_successful_socks5_connect(self):
        """Mock a successful SOCKS5 handshake + Telegram connect."""
        async def mock_open(host, port):
            reader = AsyncMock()
            writer = AsyncMock()
            writer.drain = AsyncMock()
            writer.close = lambda: None
            writer.wait_closed = AsyncMock()
            # Greeting response: version=5, method=2 (username/password)
            # Auth response: version=1, status=0 (success)
            # Connect response: 10 bytes, reply_code=0 (success)
            reader.readexactly = AsyncMock(side_effect=[
                b"\x05\x02",       # greeting
                b"\x01\x00",       # auth ok
                b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00",  # connect ok
            ])
            return reader, writer

        with patch("src.proxy_health.asyncio.open_connection", side_effect=mock_open):
            ok, err = await check_proxy_telegram("1.2.3.4", 1080, "user", "pass")

        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_connection_not_allowed(self):
        """Proxy rejects CONNECT to Telegram."""
        async def mock_open(host, port):
            reader = AsyncMock()
            writer = AsyncMock()
            writer.drain = AsyncMock()
            writer.close = lambda: None
            writer.wait_closed = AsyncMock()
            reader.readexactly = AsyncMock(side_effect=[
                b"\x05\x02",       # greeting
                b"\x01\x00",       # auth ok
                b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00",  # reply_code=2
            ])
            return reader, writer

        with patch("src.proxy_health.asyncio.open_connection", side_effect=mock_open):
            ok, err = await check_proxy_telegram("1.2.3.4", 1080, "user", "pass")

        assert ok is False
        assert "not allowed by ruleset" in err

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        """Proxy rejects credentials."""
        async def mock_open(host, port):
            reader = AsyncMock()
            writer = AsyncMock()
            writer.drain = AsyncMock()
            writer.close = lambda: None
            writer.wait_closed = AsyncMock()
            reader.readexactly = AsyncMock(side_effect=[
                b"\x05\x02",       # greeting
                b"\x01\x01",       # auth FAILED (status != 0)
            ])
            return reader, writer

        with patch("src.proxy_health.asyncio.open_connection", side_effect=mock_open):
            ok, err = await check_proxy_telegram("1.2.3.4", 1080, "user", "pass")

        assert ok is False
        assert "auth failed" in err.lower()

    @pytest.mark.asyncio
    async def test_timeout(self):
        """Connection timeout."""
        async def mock_open(host, port):
            raise asyncio.TimeoutError()

        with patch("src.proxy_health.asyncio.open_connection", side_effect=mock_open):
            ok, err = await check_proxy_telegram("1.2.3.4", 1080, timeout=0.1)

        assert ok is False
        assert "Timeout" in err

    @pytest.mark.asyncio
    async def test_no_auth_proxy(self):
        """Proxy that doesn't require auth."""
        async def mock_open(host, port):
            reader = AsyncMock()
            writer = AsyncMock()
            writer.drain = AsyncMock()
            writer.close = lambda: None
            writer.wait_closed = AsyncMock()
            reader.readexactly = AsyncMock(side_effect=[
                b"\x05\x00",       # greeting: no auth needed
                b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00",  # connect ok
            ])
            return reader, writer

        with patch("src.proxy_health.asyncio.open_connection", side_effect=mock_open):
            ok, err = await check_proxy_telegram("1.2.3.4", 1080)

        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_deep_batch_check(self, tmp_path):
        """Batch check with deep=True uses check_proxy_telegram."""
        db = Database(tmp_path / "deep.db")
        await db.initialize()
        await db.connect()
        try:
            await db.add_proxy(host="good.example.com", port=1080, username="u", password="p")
            await db.add_proxy(host="bad.example.com", port=1080, username="u", password="p")

            async def mock_tg_check(host, port, username=None, password=None, timeout=10.0):
                if "good" in host:
                    return True, None
                return False, "Connection not allowed by ruleset"

            with patch("src.proxy_health.check_proxy_telegram", side_effect=mock_tg_check):
                result = await check_proxy_batch(db, deep=True, timeout=10.0)

            assert result["alive"] == 1
            assert result["dead"] == 1
        finally:
            await db.close()


class TestCheckProxyBatch:
    @pytest_asyncio.fixture
    async def db(self, tmp_path):
        """Create a temp DB with 3 proxies."""
        db = Database(tmp_path / "test.db")
        await db.initialize()
        await db.connect()
        await db.add_proxy(host="alive1.example.com", port=1080)
        await db.add_proxy(host="alive2.example.com", port=1080)
        await db.add_proxy(host="dead1.example.com", port=1080)
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_all_alive(self, db):
        with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=True):
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)

        assert result["total"] == 3
        assert result["alive"] == 3
        assert result["dead"] == 0

        # Verify all proxies are active in DB
        proxies = await db.list_proxies()
        for p in proxies:
            assert p.status == "active"
            assert p.last_check is not None

    @pytest.mark.asyncio
    async def test_all_dead(self, db):
        with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=False):
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)

        assert result["total"] == 3
        assert result["alive"] == 0
        assert result["dead"] == 3
        assert result["changed"] == 3  # all were active, now dead

        proxies = await db.list_proxies()
        for p in proxies:
            assert p.status == "dead"

    @pytest.mark.asyncio
    async def test_mixed_results(self, db):
        async def _mock_check(host, port, timeout=5.0):
            return "alive" in host

        with patch("src.proxy_health.check_proxy_connection", side_effect=_mock_check):
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)

        assert result["total"] == 3
        assert result["alive"] == 2
        assert result["dead"] == 1
        assert result["changed"] == 1  # only dead1 changed

    @pytest.mark.asyncio
    async def test_empty_proxies(self, tmp_path):
        db = Database(tmp_path / "empty.db")
        await db.initialize()
        await db.connect()
        try:
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)
            assert result == {"total": 0, "alive": 0, "dead": 0, "changed": 0}
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_concurrency_respected(self, db):
        max_concurrent = 0
        current = 0
        lock = asyncio.Lock()

        async def _slow_check(host, port, timeout=5.0):
            nonlocal max_concurrent, current
            async with lock:
                current += 1
                if current > max_concurrent:
                    max_concurrent = current
            await asyncio.sleep(0.05)
            async with lock:
                current -= 1
            return True

        with patch("src.proxy_health.check_proxy_connection", side_effect=_slow_check):
            await check_proxy_batch(db, concurrency=2, timeout=1.0, deep=False)

        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_progress_callback_called(self, db):
        calls = []

        def on_progress(completed, total, result):
            calls.append((completed, total, result))

        with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=True):
            await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False, progress_callback=on_progress)

        assert len(calls) == 3
        # Every call should have total=3
        for _, total, r in calls:
            assert total == 3
            assert isinstance(r, ProxyCheckResult)

    @pytest.mark.asyncio
    async def test_status_change_tracked(self, tmp_path):
        """Proxy already dead stays dead → changed=0; active→dead → changed=1."""
        db = Database(tmp_path / "change.db")
        await db.initialize()
        await db.connect()
        try:
            pid1 = await db.add_proxy(host="was-active.example.com", port=1080)
            pid2 = await db.add_proxy(host="was-dead.example.com", port=1080)
            # Mark pid2 as dead already
            await db.update_proxy(pid2, status="dead")

            with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=False):
                result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)

            # was-active → dead = 1 change, was-dead → dead = 0 changes
            assert result["changed"] == 1
            assert result["dead"] == 2
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_revives_dead_proxy(self, tmp_path):
        """Dead proxy that responds should become active."""
        db = Database(tmp_path / "revive.db")
        await db.initialize()
        await db.connect()
        try:
            pid = await db.add_proxy(host="revived.example.com", port=1080)
            await db.update_proxy(pid, status="dead")

            with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=True):
                result = await check_proxy_batch(db, concurrency=10, timeout=1.0, deep=False)

            assert result["alive"] == 1
            assert result["changed"] == 1

            proxy = await db.get_proxy(pid)
            assert proxy.status == "active"
        finally:
            await db.close()
