"""Tests for ProxyManager: import, health check, replacement."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.database import Database, ProxyRecord
from src.proxy_manager import (
    ProxyManager,
    parse_proxy_line,
    proxy_record_to_string,
    update_config_proxy,
)


# ==================== parse_proxy_line ====================

class TestParseProxyLine:
    def test_socks5_full(self):
        host, port, user, pwd, proto = parse_proxy_line("socks5:1.2.3.4:1080:myuser:mypass")
        assert host == "1.2.3.4"
        assert port == 1080
        assert user == "myuser"
        assert pwd == "mypass"
        assert proto == "socks5"

    def test_host_port_user_pass(self):
        host, port, user, pwd, proto = parse_proxy_line("1.2.3.4:1080:user:pass")
        assert host == "1.2.3.4"
        assert port == 1080
        assert user == "user"
        assert pwd == "pass"
        assert proto == "socks5"

    def test_host_port_only(self):
        host, port, user, pwd, proto = parse_proxy_line("1.2.3.4:1080")
        assert host == "1.2.3.4"
        assert port == 1080
        assert user is None
        assert pwd is None

    def test_url_style(self):
        host, port, user, pwd, proto = parse_proxy_line("user:pass@1.2.3.4:1080")
        assert host == "1.2.3.4"
        assert port == 1080
        assert user == "user"
        assert pwd == "pass"

    def test_http_protocol(self):
        host, port, user, pwd, proto = parse_proxy_line("http:1.2.3.4:8080:u:p")
        assert proto == "http"
        assert host == "1.2.3.4"
        assert port == 8080

    def test_socks4_protocol(self):
        host, port, user, pwd, proto = parse_proxy_line("socks4:1.2.3.4:1080:u:p")
        assert proto == "socks4"

    def test_invalid_returns_none(self):
        host, port, user, pwd, proto = parse_proxy_line("garbage")
        assert host is None
        assert port is None

    def test_https_without_slashes(self):
        """parse_proxy_line handles 'https:host:port:user:pass' format."""
        host, port, user, pwd, proto = parse_proxy_line("https:1.2.3.4:1080:user:pass")
        assert host == "1.2.3.4"
        assert port == 1080
        assert user == "user"
        assert pwd == "pass"
        assert proto == "https"

    def test_https_with_slashes(self):
        """parse_proxy_line handles 'https://host:port' format."""
        host, port, user, pwd, proto = parse_proxy_line("https://1.2.3.4:1080")
        assert host == "1.2.3.4"
        assert port == 1080
        assert proto == "https"

    def test_port_zero_rejected(self):
        """Port 0 is invalid."""
        host, port, *_ = parse_proxy_line("1.2.3.4:0:user:pass")
        assert host is None

    def test_port_too_large_rejected(self):
        """Port > 65535 is invalid."""
        host, port, *_ = parse_proxy_line("1.2.3.4:70000:user:pass")
        assert host is None

    def test_negative_port_rejected(self):
        """Negative port is invalid (int parse succeeds but range check fails)."""
        host, port, *_ = parse_proxy_line("1.2.3.4:-1:user:pass")
        assert host is None

    # --- HTTP port auto-detect tests ---

    def test_autodetect_http_by_port_8080(self):
        """Port 8080 without explicit protocol → auto-detect http."""
        host, port, user, pwd, proto = parse_proxy_line("1.2.3.4:8080:user:pass")
        assert proto == "http"
        assert host == "1.2.3.4"
        assert port == 8080

    def test_autodetect_http_by_port_3128(self):
        """Port 3128 (Squid default) without explicit protocol → auto-detect http."""
        host, port, user, pwd, proto = parse_proxy_line("1.2.3.4:3128:user:pass")
        assert proto == "http"

    def test_autodetect_http_by_port_80(self):
        """Port 80 without explicit protocol → auto-detect http."""
        host, port, user, pwd, proto = parse_proxy_line("1.2.3.4:80")
        assert proto == "http"

    def test_autodetect_http_by_port_8888(self):
        """Port 8888 without explicit protocol → auto-detect http."""
        _, _, _, _, proto = parse_proxy_line("1.2.3.4:8888:u:p")
        assert proto == "http"

    def test_no_autodetect_on_socks_port(self):
        """Port 1080 (SOCKS default) stays socks5."""
        _, _, _, _, proto = parse_proxy_line("1.2.3.4:1080:user:pass")
        assert proto == "socks5"

    def test_explicit_socks5_overrides_http_port(self):
        """Explicit socks5: prefix keeps socks5 even on HTTP-like port."""
        _, _, _, _, proto = parse_proxy_line("socks5:1.2.3.4:8080:user:pass")
        assert proto == "socks5"

    def test_explicit_http_on_socks_port(self):
        """Explicit http: prefix stays http even on non-HTTP port."""
        _, _, _, _, proto = parse_proxy_line("http:1.2.3.4:1080:user:pass")
        assert proto == "http"

    def test_autodetect_http_url_style(self):
        """user:pass@host:port with HTTP port → auto-detect http."""
        host, port, user, pwd, proto = parse_proxy_line("user:pass@1.2.3.4:8080")
        assert proto == "http"
        assert host == "1.2.3.4"
        assert port == 8080

    def test_no_autodetect_url_style_socks_port(self):
        """user:pass@host:port with SOCKS port → stays socks5."""
        _, _, _, _, proto = parse_proxy_line("user:pass@1.2.3.4:1080")
        assert proto == "socks5"


# ==================== proxy_record_to_string ====================

class TestProxyRecordToString:
    def test_full_proxy(self):
        proxy = ProxyRecord(
            id=1, host="1.2.3.4", port=1080,
            username="user", password="pass",
            protocol="socks5", status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        assert proxy_record_to_string(proxy) == "socks5:1.2.3.4:1080:user:pass"

    def test_no_auth(self):
        proxy = ProxyRecord(
            id=1, host="1.2.3.4", port=1080,
            username=None, password=None,
            protocol="socks5", status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        assert proxy_record_to_string(proxy) == "socks5:1.2.3.4:1080"

    def test_http_protocol(self):
        proxy = ProxyRecord(
            id=1, host="proxy.com", port=8080,
            username="u", password="p",
            protocol="http", status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        assert proxy_record_to_string(proxy) == "http:proxy.com:8080:u:p"

    def test_roundtrip(self):
        """Parse -> record -> string should be consistent."""
        original = "socks5:10.0.0.1:9050:admin:secret"
        host, port, user, pwd, proto = parse_proxy_line(original)
        proxy = ProxyRecord(
            id=1, host=host, port=port,
            username=user, password=pwd,
            protocol=proto, status="active",
            assigned_account_id=None, last_check=None, created_at=None,
        )
        assert proxy_record_to_string(proxy) == original


# ==================== update_config_proxy ====================

class TestUpdateConfigProxy:
    def test_update_existing_file(self, tmp_path):
        config_path = tmp_path / "___config.json"
        config_path.write_text(json.dumps({"Name": "Test", "Proxy": "old:1:2:3:4"}), encoding="utf-8")

        update_config_proxy(config_path, "socks5:new.host:5678:u:p")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["Proxy"] == "socks5:new.host:5678:u:p"
        assert result["Name"] == "Test"

    def test_create_new_file(self, tmp_path):
        config_path = tmp_path / "new_config" / "___config.json"

        update_config_proxy(config_path, "socks5:host:1080:u:p")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["Proxy"] == "socks5:host:1080:u:p"

    def test_preserves_other_fields(self, tmp_path):
        config_path = tmp_path / "___config.json"
        original = {
            "Name": "My Account",
            "Proxy": "socks5:old:1080:u:p",
            "TonWallet": "EQxyz...",
            "CustomField": 42,
        }
        config_path.write_text(json.dumps(original), encoding="utf-8")

        update_config_proxy(config_path, "socks5:new:2080:a:b")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["Proxy"] == "socks5:new:2080:a:b"
        assert result["Name"] == "My Account"
        assert result["TonWallet"] == "EQxyz..."
        assert result["CustomField"] == 42


# ==================== ProxyManager (async) ====================

@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a temp DB."""
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.connect()
    yield db
    await db.close()


class TestImportFromFile:
    @pytest.mark.asyncio
    async def test_import_basic(self, db, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "1.1.1.1:1080:u1:p1\n"
            "2.2.2.2:1080:u2:p2\n"
            "3.3.3.3:1080:u3:p3\n",
            encoding="utf-8",
        )
        manager = ProxyManager(db, tmp_path / "accounts")
        result = await manager.import_from_file(proxy_file)

        assert result["imported"] == 3
        assert result["duplicates"] == 0
        assert result["errors"] == 0

        proxies = await db.list_proxies()
        assert len(proxies) == 3

    @pytest.mark.asyncio
    async def test_import_skips_duplicates(self, db, tmp_path):
        await db.add_proxy(host="1.1.1.1", port=1080)

        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("1.1.1.1:1080:u:p\n2.2.2.2:1080:u:p\n", encoding="utf-8")

        manager = ProxyManager(db, tmp_path / "accounts")
        result = await manager.import_from_file(proxy_file)

        assert result["imported"] == 1
        assert result["duplicates"] == 1

    @pytest.mark.asyncio
    async def test_import_skips_empty_and_comments(self, db, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "# This is a comment\n"
            "\n"
            "1.1.1.1:1080:u:p\n"
            "  \n"
            "# Another comment\n"
            "2.2.2.2:1080:u:p\n",
            encoding="utf-8",
        )
        manager = ProxyManager(db, tmp_path / "accounts")
        result = await manager.import_from_file(proxy_file)

        assert result["imported"] == 2
        assert result["errors"] == 0


class TestSyncAccountsToDb:
    @pytest.mark.asyncio
    async def test_sync_creates_accounts(self, db, tmp_path):
        accounts_dir = tmp_path / "accounts"
        for name in ["acc1", "acc2"]:
            d = accounts_dir / name
            d.mkdir(parents=True)
            (d / "session.session").touch()
            (d / "api.json").write_text('{"api_id":1,"api_hash":"h"}')
            (d / "___config.json").write_text(json.dumps({
                "Name": name,
                "Proxy": f"socks5:{name}.com:1080:u:p",
            }))

        manager = ProxyManager(db, accounts_dir)
        result = await manager.sync_accounts_to_db()

        assert result["synced"] == 2
        assert result["created"] == 2
        assert result["proxy_linked"] == 2

        accounts = await db.list_accounts()
        assert len(accounts) == 2

    @pytest.mark.asyncio
    async def test_sync_idempotent(self, db, tmp_path):
        accounts_dir = tmp_path / "accounts"
        d = accounts_dir / "acc1"
        d.mkdir(parents=True)
        (d / "session.session").touch()

        manager = ProxyManager(db, accounts_dir)
        await manager.sync_accounts_to_db()
        result = await manager.sync_accounts_to_db()

        # Second sync should not create duplicates
        accounts = await db.list_accounts()
        assert len(accounts) == 1


class TestCheckAssignedProxies:
    @pytest.mark.asyncio
    async def test_check_alive_and_dead(self, db):
        # Create accounts with proxies
        pid1 = await db.add_proxy(host="alive.com", port=1080)
        pid2 = await db.add_proxy(host="dead.com", port=1080)
        aid1, _ = await db.add_account(name="acc1", session_path="/s1")
        aid2, _ = await db.add_account(name="acc2", session_path="/s2")
        await db.assign_proxy(aid1, pid1)
        await db.assign_proxy(aid2, pid2)

        async def _mock_check(host, port, username=None, password=None, protocol="socks5", timeout=10.0):
            return ("alive" in host, None if "alive" in host else "connection refused")

        manager = ProxyManager(db)
        with patch("src.proxy_manager.check_proxy_smart", side_effect=_mock_check):
            result = await manager.check_assigned_proxies()

        assert len(result["alive"]) == 1
        assert len(result["dead"]) == 1
        assert result["alive"][0][0].name == "acc1"
        assert result["dead"][0][0].name == "acc2"

    @pytest.mark.asyncio
    async def test_accounts_without_proxy(self, db):
        await db.add_account(name="no_proxy", session_path="/s1")  # return ignored

        manager = ProxyManager(db)
        with patch("src.proxy_manager.check_proxy_smart", new_callable=AsyncMock, return_value=(True, None)):
            result = await manager.check_assigned_proxies()

        assert len(result["no_proxy"]) == 1
        assert result["no_proxy"][0] == "no_proxy"


class TestGenerateReplacementPlan:
    @pytest.mark.asyncio
    async def test_plan_with_free_proxies(self, db):
        # Dead proxy assigned to account
        pid_dead = await db.add_proxy(host="dead.com", port=1080)
        aid, _ = await db.add_account(name="acc1", session_path="/s1")
        await db.assign_proxy(aid, pid_dead)
        await db.update_proxy(pid_dead, status="dead")

        # Free proxy in pool
        await db.add_proxy(host="fresh.com", port=1080, username="u", password="p")

        account = await db.get_account(aid)
        dead_proxy = await db.get_proxy(pid_dead)

        manager = ProxyManager(db)
        plan = await manager.generate_replacement_plan([(account, dead_proxy)])

        assert len(plan) == 1
        assert plan[0]["account_name"] == "acc1"
        assert plan[0]["old_proxy"].host == "dead.com"
        assert plan[0]["new_proxy"].host == "fresh.com"

    @pytest.mark.asyncio
    async def test_plan_not_enough_proxies(self, db):
        # 2 dead proxies, only 1 free
        pid1 = await db.add_proxy(host="dead1.com", port=1080)
        pid2 = await db.add_proxy(host="dead2.com", port=1081)
        aid1, _ = await db.add_account(name="acc1", session_path="/s1")
        aid2, _ = await db.add_account(name="acc2", session_path="/s2")
        await db.assign_proxy(aid1, pid1)
        await db.assign_proxy(aid2, pid2)
        await db.update_proxy(pid1, status="dead")
        await db.update_proxy(pid2, status="dead")

        # Only 1 free proxy
        await db.add_proxy(host="fresh.com", port=1080)

        acc1 = await db.get_account(aid1)
        acc2 = await db.get_account(aid2)
        proxy1 = await db.get_proxy(pid1)
        proxy2 = await db.get_proxy(pid2)

        manager = ProxyManager(db)
        plan = await manager.generate_replacement_plan([(acc1, proxy1), (acc2, proxy2)])

        # Only 1 replacement possible
        assert len(plan) == 1


class TestExecuteReplacements:
    @pytest.mark.asyncio
    async def test_execute_updates_db_and_config(self, db, tmp_path):
        # Setup accounts dir with config
        accounts_dir = tmp_path / "accounts"
        acc_dir = accounts_dir / "acc1"
        acc_dir.mkdir(parents=True)
        config_path = acc_dir / "___config.json"
        config_path.write_text(json.dumps({
            "Name": "acc1",
            "Proxy": "socks5:dead.com:1080:u:p",
        }), encoding="utf-8")

        # DB setup
        pid_old = await db.add_proxy(host="dead.com", port=1080, username="u", password="p")
        pid_new = await db.add_proxy(host="fresh.com", port=2080, username="nu", password="np")
        aid, _ = await db.add_account(name="acc1", session_path="/s1")
        await db.assign_proxy(aid, pid_old)
        await db.update_proxy(pid_old, status="dead")
        await db.update_proxy(pid_new, status="reserved")

        old_proxy = await db.get_proxy(pid_old)
        new_proxy = await db.get_proxy(pid_new)

        plan = [{
            "account_name": "acc1",
            "account_id": aid,
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
        }]

        manager = ProxyManager(db, accounts_dir)
        result = await manager.execute_replacements(plan)

        assert result["replaced"] == 1
        assert result["errors"] == 0

        # DB: account now points to new proxy
        account = await db.get_account(aid)
        assert account.proxy_id == pid_new

        # DB: new proxy is active and assigned
        new_p = await db.get_proxy(pid_new)
        assert new_p.status == "active"
        assert new_p.assigned_account_id == aid

        # DB: old proxy is dead and unassigned
        old_p = await db.get_proxy(pid_old)
        assert old_p.status == "dead"
        assert old_p.assigned_account_id is None

        # Config file updated
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["Proxy"] == "socks5:fresh.com:2080:nu:np"
        assert config["Name"] == "acc1"

    @pytest.mark.asyncio
    async def test_execute_logs_operations(self, db, tmp_path):
        pid_old = await db.add_proxy(host="dead.com", port=1080)
        pid_new = await db.add_proxy(host="fresh.com", port=1080)
        aid, _ = await db.add_account(name="acc1", session_path="/s1")
        await db.assign_proxy(aid, pid_old)
        await db.update_proxy(pid_new, status="reserved")

        old_proxy = await db.get_proxy(pid_old)
        new_proxy = await db.get_proxy(pid_new)

        plan = [{
            "account_name": "acc1",
            "account_id": aid,
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
        }]

        manager = ProxyManager(db, tmp_path / "accounts")
        await manager.execute_replacements(plan)

        # Check operation log
        logs = await db.get_operation_log(account_id=aid, operation="proxy_replace")
        assert len(logs) == 1
        assert logs[0]["success"] is True
        assert "dead.com" in logs[0]["details"]
        assert "fresh.com" in logs[0]["details"]

    @pytest.mark.asyncio
    async def test_execute_config_write_failure_keeps_db_consistent(self, db, tmp_path):
        """If ___config.json write fails, DB should NOT be updated (file-first order)."""
        # Create a read-only config directory to force write failure
        accounts_dir = tmp_path / "accounts"
        acc_dir = accounts_dir / "acc1"
        acc_dir.mkdir(parents=True)
        config_path = acc_dir / "___config.json"
        config_path.write_text(json.dumps({
            "Name": "acc1",
            "Proxy": "socks5:dead.com:1080:u:p",
        }), encoding="utf-8")
        # Make config read-only to force write failure
        config_path.chmod(0o444)

        pid_old = await db.add_proxy(host="dead.com", port=1080, username="u", password="p")
        pid_new = await db.add_proxy(host="fresh.com", port=2080, username="nu", password="np")
        aid, _ = await db.add_account(name="acc1", session_path="/s1")
        await db.assign_proxy(aid, pid_old)
        await db.update_proxy(pid_new, status="reserved")

        old_proxy = await db.get_proxy(pid_old)
        new_proxy = await db.get_proxy(pid_new)

        plan = [{
            "account_name": "acc1",
            "account_id": aid,
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
        }]

        manager = ProxyManager(db, accounts_dir)
        result = await manager.execute_replacements(plan)

        assert result["errors"] == 1
        assert result["replaced"] == 0

        # DB should still point to old proxy (no partial update)
        account = await db.get_account(aid)
        assert account.proxy_id == pid_old

        # Restore permissions for cleanup
        config_path.chmod(0o644)

    @pytest.mark.asyncio
    async def test_execute_no_config_file_still_updates_db(self, db, tmp_path):
        """If account has no ___config.json, DB update should still succeed."""
        pid_old = await db.add_proxy(host="dead.com", port=1080)
        pid_new = await db.add_proxy(host="fresh.com", port=1080)
        aid, _ = await db.add_account(name="no_config_acc", session_path="/s1")
        await db.assign_proxy(aid, pid_old)
        await db.update_proxy(pid_new, status="reserved")

        old_proxy = await db.get_proxy(pid_old)
        new_proxy = await db.get_proxy(pid_new)

        plan = [{
            "account_name": "no_config_acc",
            "account_id": aid,
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
        }]

        manager = ProxyManager(db, tmp_path / "nonexistent_accounts")
        result = await manager.execute_replacements(plan)

        assert result["replaced"] == 1
        assert result["errors"] == 0

        # DB updated correctly
        account = await db.get_account(aid)
        assert account.proxy_id == pid_new

    @pytest.mark.asyncio
    async def test_execute_db_failure_triggers_rollback(self, db, tmp_path):
        """If DB UPDATE fails mid-transaction, rollback() is called and error is counted."""
        accounts_dir = tmp_path / "accounts"

        pid_old = await db.add_proxy(host="dead.com", port=1080)
        pid_new = await db.add_proxy(host="fresh.com", port=1080)
        aid, _ = await db.add_account(name="rollback_acc", session_path="/s1")
        await db.assign_proxy(aid, pid_old)
        await db.update_proxy(pid_new, status="reserved")

        old_proxy = await db.get_proxy(pid_old)
        new_proxy = await db.get_proxy(pid_new)

        plan = [{
            "account_name": "rollback_acc",
            "account_id": aid,
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
        }]

        manager = ProxyManager(db, accounts_dir)

        # Track rollback calls
        original_rollback = db._connection.rollback
        rollback_called = [False]

        async def tracked_rollback():
            rollback_called[0] = True
            return await original_rollback()

        # Patch only the inner DB block by making _commit_with_retry raise
        # This is cleaner: the 3 UPDATEs succeed, but commit fails → rollback
        original_commit = db._commit_with_retry
        commit_fail_active = [True]

        async def failing_commit():
            if commit_fail_active[0]:
                commit_fail_active[0] = False  # Only fail once
                raise Exception("Simulated commit failure")
            return await original_commit()

        with patch.object(db, '_commit_with_retry', side_effect=failing_commit), \
             patch.object(db._connection, 'rollback', side_effect=tracked_rollback):
            result = await manager.execute_replacements(plan)

        assert result["errors"] == 1
        assert result["replaced"] == 0
        assert rollback_called[0] is True, "rollback() should have been called on commit failure"

