"""Proxy pool management: import, health check, replacement."""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from .database import Database, ProxyRecord
from .proxy_health import check_proxy_connection, check_proxy_telegram
from .utils import mask_proxy_credentials

logger = logging.getLogger(__name__)


def parse_proxy_line(line: str) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str], str]:
    """Parse proxy line into components.

    Supported formats:
    - socks5:host:port:user:pass
    - host:port:user:pass
    - host:port
    - user:pass@host:port

    Args:
        line: Raw proxy string.

    Returns:
        Tuple of (host, port, username, password, protocol).
    """
    protocol = "socks5"

    # Remove protocol prefix if present
    if line.startswith(("socks5:", "socks4:", "http:", "https:", "https://")):
        if "://" in line:
            protocol, rest = line.split("://", 1)
            line = rest
        else:
            parts = line.split(":", 1)
            protocol = parts[0]
            line = parts[1] if len(parts) > 1 else ""

    # Handle user:pass@host:port format
    if "@" in line:
        auth_part, host_part = line.rsplit("@", 1)
        if ":" in auth_part:
            username, password = auth_part.split(":", 1)
        else:
            username, password = auth_part, None

        if ":" in host_part:
            host, port_str = host_part.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                return (None, None, None, None, protocol)
        else:
            return (None, None, None, None, protocol)

        return (host, port, username, password, protocol)

    # Handle host:port:user:pass format
    parts = line.split(":")
    if len(parts) >= 2:
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            return (None, None, None, None, protocol)
        username = parts[2] if len(parts) > 2 and parts[2] else None
        password = parts[3] if len(parts) > 3 and parts[3] else None
        return (host, port, username, password, protocol)

    return (None, None, None, None, protocol)


def proxy_record_to_string(proxy: ProxyRecord) -> str:
    """Convert DB ProxyRecord to config string.

    Args:
        proxy: ProxyRecord from database.

    Returns:
        String like "socks5:host:port:user:pass".
    """
    parts = [proxy.protocol, proxy.host, str(proxy.port)]
    if proxy.username:
        parts.append(proxy.username)
        if proxy.password:
            parts.append(proxy.password)
    return ":".join(parts)


class ProxyManager:
    """Manages proxy pool: import, health check, replacement."""

    def __init__(self, db: Database, accounts_dir: Path = Path("accounts")):
        self.db = db
        self.accounts_dir = accounts_dir
        self._config_path_cache: Optional[dict[str, Path]] = None

    async def sync_accounts_to_db(self) -> dict[str, int]:
        """Ensure all accounts from accounts/ are in DB with correct proxy_id.

        Scans account directories, creates missing accounts and proxies in DB,
        and links them together.

        Returns:
            Dict with keys: synced, created, proxy_linked.
        """
        import sqlite3

        counters = {"synced": 0, "created": 0, "proxy_linked": 0}

        if not self.accounts_dir.exists():
            return counters

        for session_file in self.accounts_dir.rglob("*.session"):
            account_dir = session_file.parent
            name = account_dir.name

            # Load config for proxy info
            config_path = account_dir / "___config.json"
            proxy_str = None
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    proxy_str = config.get("Proxy")
                    config_name = config.get("Name")
                    if config_name:
                        name = config_name
                except (json.JSONDecodeError, IOError):
                    pass

            # Add account if missing
            session_path_str = str(session_file)
            try:
                account_id = await self.db.add_account(
                    name=name,
                    session_path=session_path_str,
                )
                counters["created"] += 1
            except sqlite3.IntegrityError:
                # Already exists — find it
                accounts = await self.db.list_accounts(search=name)
                account_id = None
                for a in accounts:
                    if a.session_path == session_path_str:
                        account_id = a.id
                        break
                if account_id is None:
                    continue

            # Link proxy if present and not yet linked
            if proxy_str:
                account = await self.db.get_account(account_id)
                if account and account.proxy_id is None:
                    proxy_id = await self._find_or_create_proxy(proxy_str)
                    if proxy_id:
                        await self.db.assign_proxy(account_id, proxy_id)
                        counters["proxy_linked"] += 1

            counters["synced"] += 1

        return counters

    async def _find_or_create_proxy(self, proxy_str: str) -> Optional[int]:
        """Find existing proxy or create new one. Returns proxy ID."""
        import sqlite3

        host, port, username, password, protocol = parse_proxy_line(proxy_str)
        if not host or not port:
            return None

        # O(1) lookup via UNIQUE(host, port) index
        existing_id = await self.db.find_proxy_by_host_port(host, port)
        if existing_id is not None:
            return existing_id

        try:
            return await self.db.add_proxy(
                host=host, port=port,
                username=username, password=password,
                protocol=protocol,
            )
        except sqlite3.IntegrityError:
            # Race condition: created between check and insert
            return await self.db.find_proxy_by_host_port(host, port)

    async def import_from_file(self, file_path: Path) -> dict[str, int]:
        """Import proxies from text file.

        Each line: one proxy. Empty lines and # comments are skipped.
        Duplicates (same host:port) are skipped silently.

        Args:
            file_path: Path to text file with proxies.

        Returns:
            Dict with keys: imported, duplicates, errors.
        """
        import sqlite3

        counters = {"imported": 0, "duplicates": 0, "errors": 0}

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                host, port, username, password, protocol = parse_proxy_line(line)
                if not host or not port:
                    counters["errors"] += 1
                    logger.warning("Invalid proxy format, skipping: %s", mask_proxy_credentials(line.strip()))
                    continue

                await self.db.add_proxy(
                    host=host, port=port,
                    username=username, password=password,
                    protocol=protocol,
                )
                counters["imported"] += 1
            except sqlite3.IntegrityError:
                counters["duplicates"] += 1
            except Exception as e:
                counters["errors"] += 1
                logger.warning("Failed to import proxy line: %s", e)

        return counters

    async def check_assigned_proxies(
        self, concurrency: int = 50, timeout: float = 10.0
    ) -> dict[str, list]:
        """Health check only proxies assigned to accounts.

        Uses deep SOCKS5+Telegram check (not just TCP) to detect
        broken auth or Telegram-blocked proxies.

        Args:
            concurrency: Max concurrent checks.
            timeout: Timeout per check in seconds.

        Returns:
            Dict with keys: alive (list of (account, proxy)), dead (list of (account, proxy)),
            no_proxy (list of account names).
        """
        import asyncio

        accounts = await self.db.list_accounts()
        result: dict[str, list] = {"alive": [], "dead": [], "no_proxy": []}

        to_check: list[tuple] = []  # (account, proxy)

        for account in accounts:
            if account.proxy_id is None:
                result["no_proxy"].append(account.name)
                continue
            proxy = await self.db.get_proxy(account.proxy_id)
            if proxy is None:
                result["no_proxy"].append(account.name)
                continue
            to_check.append((account, proxy))

        if not to_check:
            return result

        sem = asyncio.Semaphore(concurrency)

        async def _check_one(account, proxy):
            async with sem:
                # FIX: Use deep SOCKS5+Telegram check instead of TCP-only
                alive, _error = await check_proxy_telegram(
                    proxy.host, proxy.port,
                    username=proxy.username,
                    password=proxy.password,
                    timeout=timeout,
                )
            new_status = "active" if alive else "dead"
            await self.db.update_proxy(proxy.id, status=new_status)
            if alive:
                result["alive"].append((account, proxy))
            else:
                result["dead"].append((account, proxy))

        await asyncio.gather(*[_check_one(a, p) for a, p in to_check])

        return result

    async def generate_replacement_plan(self, dead_list: list[tuple]) -> list[dict]:
        """Generate replacement plan for dead proxies.

        Args:
            dead_list: List of (account, proxy) tuples with dead proxies.

        Returns:
            List of dicts: [{account_name, account_id, old_proxy, new_proxy}, ...].
            If not enough free proxies, entries without new_proxy are omitted.
        """
        plan: list[dict] = []

        for account, old_proxy in dead_list:
            new_proxy = await self.db.get_free_proxy()
            if new_proxy is None:
                logger.warning("No free proxies left for %s", account.name)
                break

            plan.append({
                "account_name": account.name,
                "account_id": account.id,
                "old_proxy": old_proxy,
                "new_proxy": new_proxy,
            })

            # Reserve this proxy so it won't be picked again
            await self.db.update_proxy(new_proxy.id, status="reserved")

        return plan

    async def execute_replacements(self, plan: list[dict]) -> dict[str, int]:
        """Execute replacement plan: update DB + ___config.json.

        Args:
            plan: Output of generate_replacement_plan().

        Returns:
            Dict with keys: replaced, errors.
        """
        counters = {"replaced": 0, "errors": 0}

        for entry in plan:
            account_name = entry["account_name"]
            account_id = entry["account_id"]
            old_proxy = entry["old_proxy"]
            new_proxy = entry["new_proxy"]

            try:
                # Update ___config.json FIRST — if this fails, DB stays consistent
                config_path = self._find_config_path(account_name)
                if config_path:
                    new_proxy_str = proxy_record_to_string(new_proxy)
                    update_config_proxy(config_path, new_proxy_str)

                # Only update DB after file write succeeded
                # Atomic transaction: all three DB updates in one lock/commit
                async with self.db._db_lock:
                    try:
                        await self.db._connection.execute(
                            "UPDATE proxies SET status = 'dead', assigned_account_id = NULL WHERE id = ?",
                            (old_proxy.id,)
                        )
                        await self.db._connection.execute(
                            "UPDATE proxies SET status = 'active', assigned_account_id = ? WHERE id = ?",
                            (account_id, new_proxy.id)
                        )
                        await self.db._connection.execute(
                            "UPDATE accounts SET proxy_id = ? WHERE id = ?",
                            (new_proxy.id, account_id)
                        )
                        await self.db._commit_with_retry()
                    except Exception:
                        # Rollback partial writes INSIDE the lock
                        try:
                            await self.db._connection.rollback()
                        except Exception:
                            pass
                        raise

                # Log operation
                await self.db.log_operation(
                    account_id=account_id,
                    operation="proxy_replace",
                    success=True,
                    details=f"{old_proxy.host}:{old_proxy.port} -> {new_proxy.host}:{new_proxy.port}",
                )

                counters["replaced"] += 1
                logger.info(
                    "Replaced proxy for %s: %s:%d -> %s:%d",
                    account_name,
                    old_proxy.host, old_proxy.port,
                    new_proxy.host, new_proxy.port,
                )

            except Exception as e:
                counters["errors"] += 1
                logger.error("Failed to replace proxy for %s: %s", account_name, e)
                await self.db.log_operation(
                    account_id=account_id,
                    operation="proxy_replace",
                    success=False,
                    error_message=str(e),
                )

        return counters

    def _build_config_cache(self) -> dict[str, Path]:
        """Build mapping of account name -> ___config.json path.

        Scans once, caches for all subsequent lookups.
        """
        cache: dict[str, Path] = {}
        if not self.accounts_dir.exists():
            return cache

        for config_path in self.accounts_dir.rglob("___config.json"):
            # Index by folder name
            folder_name = config_path.parent.name
            cache[folder_name] = config_path

            # Also index by Name field from config
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                name = config.get("Name")
                if name and name != folder_name:
                    cache[name] = config_path
            except (json.JSONDecodeError, IOError):
                continue

        return cache

    def _find_config_path(self, account_name: str) -> Optional[Path]:
        """Find ___config.json for account by name.

        Uses cached mapping built on first call. O(1) per lookup.
        """
        if self._config_path_cache is None:
            self._config_path_cache = self._build_config_cache()

        return self._config_path_cache.get(account_name)


def update_config_proxy(config_path: Path, new_proxy_str: str) -> None:
    """Update Proxy field in ___config.json atomically.

    If the file exists, reads it and updates the Proxy field.
    If it doesn't exist, creates a minimal config.
    Uses temp file + rename for crash safety.

    Args:
        config_path: Path to ___config.json.
        new_proxy_str: New proxy string (e.g. "socks5:host:port:user:pass").
    """
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    config["Proxy"] = new_proxy_str

    # Atomic write: temp file in same directory, then rename
    dir_path = config_path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        # os.replace is atomic on both Unix and Windows (Python 3.3+)
        os.replace(tmp_path, config_path)
    except Exception:
        # Cleanup temp file on failure
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise
