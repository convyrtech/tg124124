"""Business logic controllers for GUI."""
import asyncio
import socket
from pathlib import Path
from typing import Optional, List, Callable
import logging
import shutil

from ..database import Database, AccountRecord, ProxyRecord

logger = logging.getLogger(__name__)


async def check_proxy_connection(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if proxy is reachable via TCP connection."""
    try:
        # Use asyncio to check TCP connection
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


class AppController:
    """Main application controller."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "tgwebauth.db"
        self.sessions_dir = data_dir / "sessions"
        self.db: Optional[Database] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def initialize(self) -> None:
        """Initialize database and directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.db_path)
        await self.db.initialize()
        await self.db.connect()

        logger.info("App initialized: %s", self.data_dir)

    async def shutdown(self) -> None:
        """Cleanup on shutdown."""
        if self.db:
            await self.db.close()

    async def get_stats(self) -> dict:
        """Get account/proxy statistics."""
        accounts = await self.db.list_accounts()
        proxies = await self.db.list_proxies()

        healthy = sum(1 for a in accounts if a.status == "healthy")
        migrating = sum(1 for a in accounts if a.status == "migrating")
        errors = sum(1 for a in accounts if a.status == "error")
        active_proxies = sum(1 for p in proxies if p.status == "active")

        return {
            "total": len(accounts),
            "healthy": healthy,
            "migrating": migrating,
            "errors": errors,
            "proxies_active": active_proxies,
            "proxies_total": len(proxies)
        }

    async def search_accounts(self, query: str) -> List[AccountRecord]:
        """Search accounts by name/username/phone."""
        return await self.db.list_accounts(search=query if query else None)

    async def check_proxy(self, proxy: ProxyRecord, timeout: float = 5.0) -> bool:
        """Check if proxy is alive."""
        return await check_proxy_connection(proxy.host, proxy.port, timeout)

    async def import_sessions(
        self,
        source_dir: Path,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> tuple[int, int]:
        """
        Import session files from directory.
        Returns: (imported_count, skipped_count)
        """
        imported = 0
        skipped = 0
        session_files = list(source_dir.glob("**/*.session"))
        total = len(session_files)

        for i, session_path in enumerate(session_files):
            try:
                # Find associated files
                account_dir = session_path.parent
                name = account_dir.name

                # Check if already exists
                existing = await self.db.list_accounts(search=name)
                if any(a.name == name for a in existing):
                    skipped += 1
                    if on_progress:
                        on_progress(i + 1, total, f"skip: {name}")
                    continue

                # Copy to sessions directory
                dest_dir = self.sessions_dir / name
                dest_dir.mkdir(exist_ok=True)

                dest_session = dest_dir / "session.session"
                shutil.copy2(session_path, dest_session)

                # Copy api.json if exists
                api_json = account_dir / "api.json"
                if api_json.exists():
                    shutil.copy2(api_json, dest_dir / "api.json")

                # Copy ___config.json if exists and parse proxy/name
                config_json = account_dir / "___config.json"
                proxy_str = None
                if config_json.exists():
                    shutil.copy2(config_json, dest_dir / "___config.json")
                    try:
                        import json
                        with open(config_json, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                            proxy_str = config.get("Proxy")
                            config_name = config.get("Name")
                            if config_name:
                                name = f"{name} ({config_name})"
                    except (json.JSONDecodeError, IOError):
                        pass

                # Add to database
                account_id = await self.db.add_account(
                    name=name,
                    session_path=str(dest_session)
                )

                # Auto-link proxy from config if available
                if proxy_str and account_id:
                    proxy_id = await self._find_or_create_proxy(proxy_str)
                    if proxy_id:
                        await self.db.assign_proxy(account_id, proxy_id)

                imported += 1

                if on_progress:
                    on_progress(i + 1, total, f"ok: {name}")

            except Exception as e:
                skipped += 1
                logger.error("Failed to import %s: %s", session_path, e)

        return imported, skipped

    async def _find_or_create_proxy(self, proxy_str: str) -> Optional[int]:
        """Find existing proxy or create new one from config string. Returns proxy ID."""
        try:
            host, port, username, password, protocol = self._parse_proxy_line(proxy_str)
            if not host or not port:
                return None

            # Check if proxy already exists
            existing = await self.db.list_proxies()
            for p in existing:
                if p.host == host and p.port == port:
                    return p.id

            # Create new proxy
            return await self.db.add_proxy(
                host=host, port=port,
                username=username, password=password,
                protocol=protocol
            )
        except Exception as e:
            logger.warning("Failed to parse/create proxy from config: %s", e)
            return None

    async def import_proxies(self, proxy_list: str) -> int:
        """
        Import proxies from text (one per line).

        Supported formats:
        - host:port:user:pass (main format)
        - host:port (no auth)
        - socks5:host:port:user:pass (with protocol)
        - user:pass@host:port (URL-style)
        """
        imported = 0
        skipped = 0
        duplicates = 0

        for line in proxy_list.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                host, port, username, password, protocol = self._parse_proxy_line(line)

                if host and port:
                    await self.db.add_proxy(
                        host=host,
                        port=port,
                        username=username,
                        password=password,
                        protocol=protocol
                    )
                    imported += 1
                else:
                    skipped += 1
                    logger.warning("Invalid proxy format: %s", line[:50])

            except Exception as e:
                error_str = str(e)
                if "UNIQUE constraint" in error_str:
                    duplicates += 1
                    logger.debug("Proxy already exists: %s:%s", host, port)
                else:
                    skipped += 1
                    logger.warning("Failed to import proxy: %s - %s", line[:50], e)

        if duplicates > 0:
            logger.info("Skipped %d duplicate proxies (already in DB)", duplicates)
        if skipped > 0:
            logger.warning("Failed to import %d proxies (invalid format)", skipped)

        return imported

    def _parse_proxy_line(self, line: str) -> tuple:
        """
        Parse proxy line into components.

        Returns: (host, port, username, password, protocol)
        """
        protocol = "socks5"  # default

        # Remove protocol prefix if present
        if line.startswith(("socks5:", "socks4:", "http:", "https://")):
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
                port = int(port_str)
            else:
                return (None, None, None, None, protocol)

            return (host, port, username, password, protocol)

        # Handle host:port:user:pass format (main format)
        parts = line.split(":")
        if len(parts) >= 2:
            host = parts[0]
            port = int(parts[1])
            username = parts[2] if len(parts) > 2 and parts[2] else None
            password = parts[3] if len(parts) > 3 and parts[3] else None
            return (host, port, username, password, protocol)

        return (None, None, None, None, protocol)
