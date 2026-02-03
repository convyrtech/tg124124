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

    @pytest.mark.asyncio
    async def test_migration_tracking(self, db_path):
        """Test migration state persistence."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add account
            account_id = await db.add_account(
                name="Test Account",
                session_path="/test.session"
            )

            # Start migration
            migration_id = await db.start_migration(account_id)
            assert migration_id > 0

            # Account should be 'migrating'
            account = await db.get_account(account_id)
            assert account.status == "migrating"

            # Complete migration successfully
            await db.complete_migration(
                migration_id,
                success=True,
                profile_path="/profiles/test"
            )

            # Account should be 'healthy'
            account = await db.get_account(account_id)
            assert account.status == "healthy"

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_failure_tracking(self, db_path):
        """Test migration failure is tracked correctly."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            account_id = await db.add_account(
                name="Failing Account",
                session_path="/fail.session"
            )

            migration_id = await db.start_migration(account_id)

            # Complete migration with failure
            await db.complete_migration(
                migration_id,
                success=False,
                error_message="QR decode failed"
            )

            # Account should be 'error' with message
            account = await db.get_account(account_id)
            assert account.status == "error"
            assert account.error_message == "QR decode failed"

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_reset_interrupted_migrations(self, db_path):
        """Test that interrupted migrations can be reset."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Create account and start migration (don't complete)
            account_id = await db.add_account(
                name="Interrupted",
                session_path="/interrupted.session"
            )
            await db.start_migration(account_id)

            # Verify it's migrating
            account = await db.get_account(account_id)
            assert account.status == "migrating"

            # Get incomplete migrations
            incomplete = await db.get_incomplete_migrations()
            assert len(incomplete) == 1

            # Reset interrupted
            count = await db.reset_interrupted_migrations()
            assert count == 1

            # Account should be back to pending
            account = await db.get_account(account_id)
            assert account.status == "pending"

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_update_account_sql_injection_protection(self, db_path):
        """Test that SQL injection via field names is blocked."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            account_id = await db.add_account(
                name="Test",
                session_path="/test.session"
            )

            # Try to inject via field name
            with pytest.raises(ValueError, match="Invalid account fields"):
                await db.update_account(
                    account_id,
                    **{"name = 'hacked'; DROP TABLE accounts; --": "value"}
                )

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_update_proxy_sql_injection_protection(self, db_path):
        """Test that SQL injection via proxy field names is blocked."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            proxy_id = await db.add_proxy(
                host="1.1.1.1",
                port=1080
            )

            # Try to inject via field name
            with pytest.raises(ValueError, match="Invalid proxy fields"):
                await db.update_proxy(
                    proxy_id,
                    **{"host = '1.1.1.1'; DROP TABLE proxies; --": "value"}
                )

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_stats(self, db_path):
        """Test migration statistics."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Add accounts with various statuses
            await db.add_account(name="A1", session_path="/a1.session", status="pending")
            await db.add_account(name="A2", session_path="/a2.session", status="healthy")
            await db.add_account(name="A3", session_path="/a3.session", status="healthy")
            await db.add_account(name="A4", session_path="/a4.session", status="error")

            stats = await db.get_migration_stats()

            assert stats["total"] == 4
            assert stats["pending"] == 1
            assert stats["healthy"] == 2
            assert stats["error"] == 1
            assert stats["success_rate"] == pytest.approx(66.67, rel=0.1)

        finally:
            await db.close()
