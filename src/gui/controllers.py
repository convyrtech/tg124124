"""Business logic controllers for GUI."""
import asyncio
import socket
from pathlib import Path
from typing import Optional, List, Callable
import logging
import shutil

from ..database import Database, AccountRecord, ProxyRecord
from ..proxy_manager import parse_proxy_line

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
        from ..paths import ACCOUNTS_DIR
        self.sessions_dir = ACCOUNTS_DIR
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
        """Get account/proxy statistics.

        FIX-7.2: Uses SQL COUNT(*) aggregation instead of loading all records.
        """
        return await self.db.get_counts()

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

        Supports formats:
        - Standard: account_dir/session.session + api.json + ___config.json
        - Lolzteam: account_dir/session.session + api.json (no config)
        """
        imported = 0
        skipped = 0
        session_files = list(source_dir.glob("**/*.session"))
        total = len(session_files)

        if total == 0:
            logger.warning("No .session files found in %s", source_dir)
            if on_progress:
                on_progress(0, 0, "No .session files found")
            return 0, 0

        # Pre-load all existing account names for O(1) dedup lookup
        all_accounts = await self.db.list_accounts()
        existing_names = {a.name for a in all_accounts}
        # Also track base names (without config suffix) to catch renamed accounts
        existing_base_names = set()
        for a in all_accounts:
            existing_base_names.add(a.name)
            # Extract base name from "folder_name (config_name)" format
            if " (" in a.name and a.name.endswith(")"):
                base = a.name.rsplit(" (", 1)[0]
                existing_base_names.add(base)

        logger.info("Found %d .session files to import, %d accounts already in DB",
                     total, len(existing_names))

        for i, session_path in enumerate(session_files):
            try:
                # Find associated files
                account_dir = session_path.parent
                base_name = account_dir.name

                # Parse config first to determine final name
                config_json = account_dir / "___config.json"
                proxy_str = None
                config_name = None
                if config_json.exists():
                    try:
                        import json
                        with open(config_json, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                            proxy_str = config.get("Proxy")
                            config_name = config.get("Name")
                    except (json.JSONDecodeError, IOError) as e:
                        logger.warning("Failed to parse config %s: %s", config_json, e)

                # Build final name: "folder (config_name)" only if config_name differs
                if config_name and config_name != base_name:
                    name = f"{base_name} ({config_name})"
                else:
                    name = base_name

                # Dedup check: by final name AND by base folder name
                if name in existing_names or base_name in existing_base_names:
                    skipped += 1
                    if on_progress:
                        on_progress(i + 1, total, f"skip (exists): {name}")
                    continue

                # Validate session file size (must be non-empty SQLite)
                if session_path.stat().st_size < 1024:
                    skipped += 1
                    reason = f"skip (too small {session_path.stat().st_size}B): {name}"
                    logger.warning("Session file too small: %s (%d bytes)",
                                   session_path, session_path.stat().st_size)
                    if on_progress:
                        on_progress(i + 1, total, reason)
                    continue

                # Copy to sessions directory
                dest_dir = self.sessions_dir / base_name
                dest_dir.mkdir(exist_ok=True)

                dest_session = dest_dir / "session.session"
                shutil.copy2(session_path, dest_session)

                # Copy api.json if exists
                api_json = account_dir / "api.json"
                if api_json.exists():
                    shutil.copy2(api_json, dest_dir / "api.json")

                # Copy ___config.json if exists
                if config_json.exists():
                    shutil.copy2(config_json, dest_dir / "___config.json")

                # Add to database
                account_id = await self.db.add_account(
                    name=name,
                    session_path=str(dest_session)
                )

                # Track in dedup sets for this batch
                existing_names.add(name)
                existing_base_names.add(base_name)

                # Auto-link proxy from config if available
                if proxy_str and proxy_str.strip() and account_id:
                    proxy_id = await self._find_or_create_proxy(proxy_str)
                    if proxy_id:
                        await self.db.assign_proxy(account_id, proxy_id)

                imported += 1

                if on_progress:
                    on_progress(i + 1, total, f"ok: {name}")

            except Exception as e:
                skipped += 1
                logger.error("Failed to import %s: %s", session_path, e, exc_info=True)
                if on_progress:
                    on_progress(i + 1, total, f"error: {account_dir.name} - {e}")

        logger.info("Import complete: %d imported, %d skipped", imported, skipped)
        return imported, skipped

    async def _find_or_create_proxy(self, proxy_str: str) -> Optional[int]:
        """Find existing proxy or create new one from config string. Returns proxy ID."""
        try:
            host, port, username, password, protocol = self._parse_proxy_line(proxy_str)
            if not host or not port:
                return None

            # O(1) lookup via UNIQUE(host, port) index
            existing_id = await self.db.find_proxy_by_host_port(host, port)
            if existing_id is not None:
                return existing_id

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
                    logger.debug("Proxy already exists: %s", line[:50])
                else:
                    skipped += 1
                    logger.warning("Failed to import proxy: %s - %s", line[:50], e)

        if duplicates > 0:
            logger.info("Skipped %d duplicate proxies (already in DB)", duplicates)
        if skipped > 0:
            logger.warning("Failed to import %d proxies (invalid format)", skipped)

        return imported

    def _parse_proxy_line(self, line: str) -> tuple:
        """Parse proxy line into components.

        Delegates to proxy_manager.parse_proxy_line.

        Returns: (host, port, username, password, protocol)
        """
        return parse_proxy_line(line)
