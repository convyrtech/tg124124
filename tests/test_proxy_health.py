"""Tests for proxy health check batch."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from src.database import Database
from src.proxy_health import check_proxy_batch, ProxyCheckResult


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
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0)

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
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0)

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
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0)

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
            result = await check_proxy_batch(db, concurrency=10, timeout=1.0)
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
            await check_proxy_batch(db, concurrency=2, timeout=1.0)

        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_progress_callback_called(self, db):
        calls = []

        def on_progress(completed, total, result):
            calls.append((completed, total, result))

        with patch("src.proxy_health.check_proxy_connection", new_callable=AsyncMock, return_value=True):
            await check_proxy_batch(db, concurrency=10, timeout=1.0, progress_callback=on_progress)

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
                result = await check_proxy_batch(db, concurrency=10, timeout=1.0)

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
                result = await check_proxy_batch(db, concurrency=10, timeout=1.0)

            assert result["alive"] == 1
            assert result["changed"] == 1

            proxy = await db.get_proxy(pid)
            assert proxy.status == "active"
        finally:
            await db.close()
