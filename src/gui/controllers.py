"""Business logic controllers for GUI."""
import asyncio
from pathlib import Path
from typing import Optional, List, Callable
import logging
import shutil

from ..database import Database, AccountRecord, ProxyRecord

logger = logging.getLogger(__name__)


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

    async def import_sessions(
        self,
        source_dir: Path,
        on_progress: Optional[Callable[[int, int], None]] = None
    ) -> int:
        """Import session files from directory."""
        imported = 0
        session_files = list(source_dir.glob("**/*.session"))
        total = len(session_files)

        for i, session_path in enumerate(session_files):
            try:
                # Find associated files
                account_dir = session_path.parent
                name = account_dir.name

                # Copy to sessions directory
                dest_dir = self.sessions_dir / name
                dest_dir.mkdir(exist_ok=True)

                dest_session = dest_dir / "session.session"
                shutil.copy2(session_path, dest_session)

                # Copy api.json if exists
                api_json = account_dir / "api.json"
                if api_json.exists():
                    shutil.copy2(api_json, dest_dir / "api.json")

                # Add to database
                await self.db.add_account(
                    name=name,
                    session_path=str(dest_session)
                )

                imported += 1

                if on_progress:
                    on_progress(i + 1, total)

            except Exception as e:
                logger.error("Failed to import %s: %s", session_path, e)

        return imported

    async def import_proxies(self, proxy_list: str) -> int:
        """Import proxies from text (one per line, format: host:port:user:pass)."""
        imported = 0

        for line in proxy_list.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                parts = line.split(":")
                if len(parts) >= 2:
                    host = parts[0]
                    port = int(parts[1])
                    username = parts[2] if len(parts) > 2 else None
                    password = parts[3] if len(parts) > 3 else None

                    await self.db.add_proxy(
                        host=host,
                        port=port,
                        username=username,
                        password=password
                    )
                    imported += 1
            except Exception as e:
                logger.warning("Failed to parse proxy line: %s - %s", line, e)

        return imported
