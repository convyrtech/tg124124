# tests/test_database.py
import pytest
import asyncio
from pathlib import Path


class TestDatabase:
    @pytest.fixture
    def db_path(self, tmp_path):
        return tmp_path / "test.db"

    def test_create_tables(self, db_path):
        from src.database import Database

        db = Database(db_path)
        asyncio.run(db.initialize())

        # Check tables exist
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "accounts" in tables
        assert "proxies" in tables
        assert "migrations" in tables

    @pytest.mark.asyncio
    async def test_add_and_get_account(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add account
            account_id = await db.add_account(
                name="Test Account",
                session_path="/path/to/session.session",
                phone="+1234567890"
            )

            assert account_id > 0

            # Get account
            account = await db.get_account(account_id)

            assert account is not None
            assert account.name == "Test Account"
            assert account.phone == "+1234567890"
            assert account.status == "pending"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_list_accounts_with_filter(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            await db.add_account(name="Account 1", session_path="/a.session", status="healthy")
            await db.add_account(name="Account 2", session_path="/b.session", status="error")
            await db.add_account(name="Account 3", session_path="/c.session", status="healthy")

            all_accounts = await db.list_accounts()
            assert len(all_accounts) == 3

            healthy = await db.list_accounts(status="healthy")
            assert len(healthy) == 2
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_add_proxy_and_assign(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add proxy
            proxy_id = await db.add_proxy(
                host="192.168.1.1",
                port=1080,
                username="user",
                password="pass"
            )
            assert proxy_id > 0

            # Add account
            account_id = await db.add_account(
                name="Test",
                session_path="/test.session"
            )

            # Assign proxy to account
            await db.assign_proxy(account_id, proxy_id)

            # Verify
            account = await db.get_account(account_id)
            assert account.proxy_id == proxy_id

            proxy = await db.get_proxy(proxy_id)
            assert proxy.assigned_account_id == account_id
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_get_free_proxy(self, db_path):
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add proxies
            p1 = await db.add_proxy(host="1.1.1.1", port=1080)
            p2 = await db.add_proxy(host="2.2.2.2", port=1080)

            # Get free proxy
            free = await db.get_free_proxy()
            assert free is not None
            assert free.id in [p1, p2]

            # Assign it
            acc = await db.add_account(name="A", session_path="/a.session")
            await db.assign_proxy(acc, free.id)

            # Get another free proxy
            free2 = await db.get_free_proxy()
            assert free2 is not None
            assert free2.id != free.id
        finally:
            await db.close()
