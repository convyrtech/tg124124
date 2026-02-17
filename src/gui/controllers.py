"""Business logic controllers for GUI."""

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from ..database import AccountRecord, Database, ProxyRecord
from ..proxy_manager import parse_proxy_line

logger = logging.getLogger(__name__)


async def check_proxy_connection(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if proxy is reachable via TCP connection."""
    try:
        # Use asyncio to check TCP connection
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
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
        self.db: Database | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._import_lock = asyncio.Lock()

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

    async def search_accounts(self, query: str) -> list[AccountRecord]:
        """Search accounts by name/username/phone."""
        return await self.db.list_accounts(search=query if query else None)

    async def check_proxy(self, proxy: ProxyRecord, timeout: float = 5.0) -> bool:
        """Check if proxy is alive (protocol-aware: SOCKS5 or HTTP CONNECT)."""
        from ..proxy_health import check_proxy_smart

        alive, _error = await check_proxy_smart(
            proxy.host, proxy.port,
            username=proxy.username,
            password=proxy.password,
            protocol=proxy.protocol or "socks5",
            timeout=timeout,
        )
        return alive

    async def import_sessions(
        self, source_dir: Path, on_progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[int, int]:
        """
        Import session files from directory.
        Returns: (imported_count, skipped_count)

        Supports formats:
        - Standard: account_dir/session.session + api.json + ___config.json
        - Lolzteam: account_dir/session.session + api.json (no config)

        FIX #11: Heavy file I/O (glob, stat, copy2, json.load) is offloaded
        to a thread executor so the asyncio event loop is not blocked.
        """
        # Serialize concurrent calls to prevent duplicate imports
        if self._import_lock.locked():
            logger.warning("import_sessions already running, skipping")
            return 0, 0
        async with self._import_lock:
            return await self._import_sessions_inner(source_dir, on_progress)

    async def _import_sessions_inner(
        self, source_dir: Path, on_progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[int, int]:
        """Inner import logic, called under _import_lock."""
        import json as json_mod

        loop = asyncio.get_running_loop()

        # Offload glob to executor (can be slow on network drives / large dirs)
        session_files = await loop.run_in_executor(None, lambda: list(source_dir.glob("**/*.session")))
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

        logger.info("Found %d .session files to import, %d accounts already in DB", total, len(existing_names))

        imported = 0
        skipped = 0

        for i, session_path in enumerate(session_files):
            try:
                # Find associated files
                account_dir = session_path.parent
                base_name = account_dir.name

                # Lolz flat format: "123456_telethon.session" in shared dir
                # If session filename != "session.session", use stem as account name
                is_flat = session_path.name != "session.session"
                if is_flat:
                    # Extract user ID from filename like "191797833_telethon.session"
                    stem = session_path.stem  # "191797833_telethon"
                    base_name = stem.split("_")[0] if "_" in stem else stem

                # Parse config first to determine final name
                config_json = account_dir / "___config.json"
                proxy_str = None
                config_name = None
                if config_json.exists():
                    try:

                        def _read_config(p: Path) -> dict:
                            with open(p, encoding="utf-8") as f:
                                return json_mod.load(f)

                        config = await loop.run_in_executor(None, _read_config, config_json)
                        proxy_str = config.get("Proxy")
                        config_name = config.get("Name")
                    except (OSError, json_mod.JSONDecodeError) as e:
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
                file_size = await loop.run_in_executor(None, lambda p=session_path: p.stat().st_size)
                if file_size < 1024:
                    skipped += 1
                    reason = f"skip (too small {file_size}B): {name}"
                    logger.warning("Session file too small: %s (%d bytes)", session_path, file_size)
                    if on_progress:
                        on_progress(i + 1, total, reason)
                    continue

                # Copy to sessions directory (offloaded to executor)
                dest_dir = self.sessions_dir / base_name
                dest_session = dest_dir / "session.session"

                # Default API credentials (Telegram Desktop) for sessions without api.json
                _DEFAULT_API = {
                    "api_id": 2040,
                    "api_hash": "b18441a1ff607e10a989891a5462e627",
                    "device_model": "Desktop",
                    "system_version": "Windows 10",
                    "app_version": "5.5.2 x64",
                    "lang_code": "en",
                    "system_lang_code": "en-US",
                }

                def _copy_files(src_session, src_dir, dst_dir, dst_session, flat):
                    dst_dir.mkdir(exist_ok=True)
                    # Skip copy if source and destination are the same file
                    if not (dst_session.exists() and os.path.samefile(src_session, dst_session)):
                        shutil.copy2(src_session, dst_session)
                    # Copy api.json (from same dir for structured, or create default for flat)
                    api_json = src_dir / "api.json"
                    dst_api = dst_dir / "api.json"
                    if api_json.exists():
                        if not (dst_api.exists() and os.path.samefile(api_json, dst_api)):
                            shutil.copy2(api_json, dst_api)
                    elif not dst_api.exists():
                        # No api.json — create with default Telegram Desktop credentials
                        with open(dst_api, "w", encoding="utf-8") as f:
                            json_mod.dump(_DEFAULT_API, f, indent=2)
                    cfg_json = src_dir / "___config.json"
                    if cfg_json.exists():
                        dst_cfg = dst_dir / "___config.json"
                        if not (dst_cfg.exists() and os.path.samefile(cfg_json, dst_cfg)):
                            shutil.copy2(cfg_json, dst_cfg)

                await loop.run_in_executor(
                    None, _copy_files, session_path, account_dir, dest_dir, dest_session, is_flat
                )

                # Add to database — cleanup dest_dir on failure
                from ..paths import to_relative_path

                try:
                    account_id = await self.db.add_account(name=name, session_path=to_relative_path(dest_session))
                except Exception:
                    # Rollback: remove copied files to prevent orphaned dirs
                    try:
                        shutil.rmtree(dest_dir, ignore_errors=True)
                    except Exception:
                        pass
                    raise

                # Track in dedup sets for this batch
                existing_names.add(name)
                existing_base_names.add(base_name)

                # Auto-link proxy from config if available
                proxy_ok = False
                if proxy_str and proxy_str.strip() and account_id:
                    try:
                        proxy_id = await self._find_or_create_proxy(proxy_str)
                        if proxy_id:
                            await self.db.assign_proxy(account_id, proxy_id)
                            proxy_ok = True
                    except Exception as proxy_err:
                        logger.warning(
                            "Proxy assignment skipped for %s: %s (account imported without proxy)",
                            name, proxy_err,
                        )

                imported += 1

                if on_progress:
                    suffix = "" if proxy_ok or not proxy_str else " (без прокси)"
                    on_progress(i + 1, total, f"ok: {name}{suffix}")

            except Exception as e:
                skipped += 1
                logger.error("Failed to import %s: %s", session_path, e, exc_info=True)
                if on_progress:
                    on_progress(i + 1, total, f"error: {account_dir.name} - {e}")

        logger.info("Import complete: %d imported, %d skipped", imported, skipped)
        return imported, skipped

    # Ports that typically indicate HTTP/HTTPS proxies, not SOCKS5
    _HTTP_PROXY_PORTS = {80, 8080, 8888, 3128, 3129}

    async def _find_or_create_proxy(self, proxy_str: str) -> int | None:
        """Find existing proxy or create new one from config string. Returns proxy ID."""
        try:
            host, port, username, password, protocol = self._parse_proxy_line(proxy_str)
            if not host or not port:
                return None

            # Warn if port suggests HTTP proxy but protocol defaulted to socks5
            if port in self._HTTP_PROXY_PORTS and protocol == "socks5":
                logger.warning(
                    "Proxy %s:%d looks like HTTP (port %d) but will be used as SOCKS5. "
                    "If migration fails, specify protocol explicitly: http:%s:%d:user:pass",
                    host, port, port, host, port,
                )

            # O(1) lookup via UNIQUE(host, port) index
            existing_id = await self.db.find_proxy_by_host_port(host, port)
            if existing_id is not None:
                return existing_id

            # Create new proxy — retry lookup on UNIQUE constraint violation
            try:
                return await self.db.add_proxy(
                    host=host, port=port, username=username, password=password, protocol=protocol
                )
            except Exception:
                # Another coroutine may have created it between check and insert
                existing_id = await self.db.find_proxy_by_host_port(host, port)
                if existing_id is not None:
                    return existing_id
                raise
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

        for idx, line in enumerate(proxy_list.strip().split("\n")):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                host, port, username, password, protocol = self._parse_proxy_line(line)

                if host and port:
                    await self.db.add_proxy(
                        host=host, port=port, username=username, password=password, protocol=protocol
                    )
                    imported += 1
                else:
                    skipped += 1
                    logger.warning("Invalid proxy format (line %d)", idx + 1)

            except Exception as e:
                error_str = str(e)
                if "UNIQUE constraint" in error_str:
                    duplicates += 1
                    logger.debug("Proxy already exists (line %d)", idx + 1)
                else:
                    skipped += 1
                    logger.warning("Failed to import proxy (line %d): %s", idx + 1, type(e).__name__)

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
