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
            account_id, _ = await db.add_account(
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
            await db.add_account(name="Account 1", session_path="/a.session", status="healthy")  # return ignored
            await db.add_account(name="Account 2", session_path="/b.session", status="error")  # return ignored
            await db.add_account(name="Account 3", session_path="/c.session", status="healthy")  # return ignored

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
            account_id, _ = await db.add_account(
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
            acc, _ = await db.add_account(name="A", session_path="/a.session")
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
            account_id, _ = await db.add_account(
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
            account_id, _ = await db.add_account(
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
            account_id, _ = await db.add_account(
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
            account_id, _ = await db.add_account(
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
            await db.add_account(name="A1", session_path="/a1.session", status="pending")  # return ignored
            await db.add_account(name="A2", session_path="/a2.session", status="healthy")  # return ignored
            await db.add_account(name="A3", session_path="/a3.session", status="healthy")  # return ignored
            await db.add_account(name="A4", session_path="/a4.session", status="error")  # return ignored

            stats = await db.get_migration_stats()

            assert stats["total"] == 4
            assert stats["pending"] == 1
            assert stats["healthy"] == 2
            assert stats["error"] == 1
            assert stats["success_rate"] == pytest.approx(66.67, rel=0.1)

        finally:
            await db.close()

    # ==================== New schema tests ====================

    def test_schema_new_columns(self, db_path):
        """Test that fragment_status, web_last_verified, auth_ttl_days columns exist."""
        from src.database import Database
        import sqlite3

        db = Database(db_path)
        asyncio.run(db.initialize())

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(accounts)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "fragment_status" in columns
        assert "web_last_verified" in columns
        assert "auth_ttl_days" in columns

    def test_schema_new_tables(self, db_path):
        """Test that batches and operation_log tables exist."""
        from src.database import Database
        import sqlite3

        db = Database(db_path)
        asyncio.run(db.initialize())

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "batches" in tables
        assert "operation_log" in tables

    def test_schema_migrations_batch_id(self, db_path):
        """Test that migrations table has batch_id column."""
        from src.database import Database
        import sqlite3

        db = Database(db_path)
        asyncio.run(db.initialize())

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(migrations)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "batch_id" in columns

    def test_alter_table_idempotent(self, db_path):
        """Test that re-running initialize() doesn't crash."""
        from src.database import Database

        db = Database(db_path)
        asyncio.run(db.initialize())
        # Second run should not raise
        asyncio.run(db.initialize())

        # Third run for good measure
        asyncio.run(db.initialize())

    # ==================== Batch lifecycle tests ====================

    @pytest.mark.asyncio
    async def test_batch_lifecycle(self, db_path):
        """Test start batch, mark completed/failed, get pending, finish."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Create accounts first
            await db.add_account(name="Acc1", session_path="/acc1.session")  # return ignored
            await db.add_account(name="Acc2", session_path="/acc2.session")  # return ignored
            await db.add_account(name="Acc3", session_path="/acc3.session")  # return ignored

            # Start batch
            batch_id = await db.start_batch(["Acc1", "Acc2", "Acc3"])
            assert batch_id  # non-empty string

            # Get active batch
            active = await db.get_active_batch()
            assert active is not None
            assert active["batch_id"] == batch_id
            assert active["total_count"] == 3

            db_batch_id = active["id"]

            # All three should be pending
            pending = await db.get_batch_pending(db_batch_id)
            assert len(pending) == 3
            assert set(pending) == {"Acc1", "Acc2", "Acc3"}

            # Mark one completed
            await db.mark_batch_account_completed(db_batch_id, "Acc1")
            pending = await db.get_batch_pending(db_batch_id)
            assert len(pending) == 2
            assert "Acc1" not in pending

            # Mark one failed
            await db.mark_batch_account_failed(db_batch_id, "Acc2", "QR timeout")
            pending = await db.get_batch_pending(db_batch_id)
            assert pending == ["Acc3"]

            # Check failed
            failed = await db.get_batch_failed(db_batch_id)
            assert len(failed) == 1
            assert failed[0]["account"] == "Acc2"
            assert failed[0]["error"] == "QR timeout"

            # Finish batch
            await db.finish_batch(db_batch_id)
            active = await db.get_active_batch()
            assert active is None  # no more active batches

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_batch_status(self, db_path):
        """Test batch status summary."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # No batches yet
            status = await db.get_batch_status()
            assert status is None

            # Create accounts and batch
            await db.add_account(name="X1", session_path="/x1.session")  # return ignored
            await db.add_account(name="X2", session_path="/x2.session")  # return ignored
            await db.start_batch(["X1", "X2"])

            status = await db.get_batch_status()
            assert status is not None
            assert status["has_batch"] is True
            assert status["total"] == 2
            assert status["completed"] == 0
            assert status["failed"] == 0
            assert status["pending"] == 2
            assert status["is_finished"] is False

            # Mark one completed
            await db.mark_batch_account_completed(status["batch_db_id"], "X1")
            status = await db.get_batch_status()
            assert status["completed"] == 1
            assert status["pending"] == 1

        finally:
            await db.close()

    # ==================== Operation log tests ====================

    @pytest.mark.asyncio
    async def test_operation_log(self, db_path):
        """Test write and read operation log."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            acc_id, _ = await db.add_account(name="LogTest", session_path="/log.session")

            # Log success
            await db.log_operation(acc_id, "qr_login", True, details="token=abc")

            # Log failure
            await db.log_operation(acc_id, "qr_login", False, error_message="QR decode failed")

            # Log without account
            await db.log_operation(None, "batch_start", True, details="batch_123")

            # Read all
            logs = await db.get_operation_log()
            assert len(logs) == 3

            # Filter by account
            acc_logs = await db.get_operation_log(account_id=acc_id)
            assert len(acc_logs) == 2

            # Filter by operation
            qr_logs = await db.get_operation_log(operation="qr_login")
            assert len(qr_logs) == 2

            # Check log content
            success_log = [l for l in acc_logs if l["success"]][0]
            assert success_log["operation"] == "qr_login"
            assert success_log["details"] == "token=abc"

            fail_log = [l for l in acc_logs if not l["success"]][0]
            assert fail_log["error_message"] == "QR decode failed"

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_update_account_new_fields(self, db_path):
        """Test that new account fields can be updated."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            acc_id, _ = await db.add_account(name="FragTest", session_path="/frag.session")

            # Update fragment_status
            await db.update_account(acc_id, fragment_status="authorized")
            await db.update_account(acc_id, auth_ttl_days=30)

            # Verify via raw SQL (AccountRecord doesn't expose new fields yet)
            async with db._connection.execute(
                "SELECT fragment_status, auth_ttl_days FROM accounts WHERE id = ?",
                (acc_id,)
            ) as cursor:
                row = await cursor.fetchone()
                assert row["fragment_status"] == "authorized"
                assert row["auth_ttl_days"] == 30

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_remove_duplicate_accounts_keeps_richest(self, db_path):
        """remove_duplicate_accounts keeps the record with proxy/status/fragment."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Insert 3 accounts with same name via raw SQL (bypass add_account dedup)
            # Account 1: poorest — no proxy, pending, no fragment
            async with db._connection.execute(
                "INSERT INTO accounts (name, session_path, status) VALUES (?, ?, ?)",
                ("dup_test", "/s1.session", "pending")
            ) as cur:
                id1 = cur.lastrowid
            # Account 2: has proxy
            async with db._connection.execute(
                "INSERT INTO accounts (name, session_path, status) VALUES (?, ?, ?)",
                ("dup_test", "/s2.session", "pending")
            ) as cur:
                id2 = cur.lastrowid
            proxy_id = await db.add_proxy(host="1.2.3.4", port=1080)
            await db.assign_proxy(id2, proxy_id)

            # Account 3: richest — has proxy, non-pending status, fragment
            async with db._connection.execute(
                "INSERT INTO accounts (name, session_path, status) VALUES (?, ?, ?)",
                ("dup_test", "/s3.session", "pending")
            ) as cur:
                id3 = cur.lastrowid
            proxy_id2 = await db.add_proxy(host="5.6.7.8", port=1080)
            await db.assign_proxy(id3, proxy_id2)
            await db.update_account(id3, status="success", fragment_status="authorized")
            await db._commit_with_retry()

            # Verify we have 3 duplicates
            all_accs = await db.list_accounts()
            dup_names = [a for a in all_accs if a.name == "dup_test"]
            assert len(dup_names) == 3

            # Run dedup
            removed = await db.remove_duplicate_accounts()
            assert removed == 2  # Should remove 2, keep 1

            # Verify the richest survived
            all_accs = await db.list_accounts()
            dup_names = [a for a in all_accs if a.name == "dup_test"]
            assert len(dup_names) == 1
            survivor = dup_names[0]
            assert survivor.id == id3  # Richest record kept
            assert survivor.proxy_id == proxy_id2

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_db_lock_serializes_writes(self, db_path):
        """Concurrent writes don't corrupt DB thanks to _db_lock."""
        import asyncio
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            # Run 20 concurrent add_account calls
            tasks = []
            for i in range(20):
                tasks.append(db.add_account(
                    name=f"concurrent_{i}",
                    session_path=f"/path/session_{i}.session"
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # All should succeed (no OperationalError: database is locked)
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 0, f"Concurrent writes failed: {errors}"

            # Verify all 20 were added
            accounts = await db.list_accounts()
            concurrent_accs = [a for a in accounts if a.name.startswith("concurrent_")]
            assert len(concurrent_accs) == 20

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_delete_proxy_clears_account_proxy_id(self, db_path):
        """delete_proxy must clear accounts.proxy_id to prevent IP leaks."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            acc_id, _ = await db.add_account(name="ProxyTest", session_path="/s.session")
            proxy_id = await db.add_proxy(host="10.0.0.1", port=1080)
            await db.assign_proxy(acc_id, proxy_id)

            # Verify proxy is assigned
            acc = await db.get_account(acc_id)
            assert acc.proxy_id == proxy_id

            # Delete the proxy
            await db.delete_proxy(proxy_id)

            # Account's proxy_id should be cleared
            acc = await db.get_account(acc_id)
            assert acc.proxy_id is None, "delete_proxy must clear accounts.proxy_id"

        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_foreign_keys_enabled(self, db_path):
        """PRAGMA foreign_keys=ON should be set after connect."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            async with db._connection.execute("PRAGMA foreign_keys") as cursor:
                row = await cursor.fetchone()
                assert row[0] == 1, "foreign_keys PRAGMA should be ON"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_list_accounts_escapes_like_wildcards(self, db_path):
        """LIKE wildcards (%, _) in search must be treated as literals."""
        from src.database import Database

        db = Database(db_path)
        await db.initialize()
        await db.connect()

        try:
            await db.add_account(name="test_user", session_path="/t1.session")  # return ignored
            await db.add_account(name="testXuser", session_path="/t2.session")  # return ignored

            # Search for literal underscore — should NOT match "testXuser"
            results = await db.list_accounts(search="test_user")
            names = [a.name for a in results]
            assert "test_user" in names
            assert "testXuser" not in names

        finally:
            await db.close()
