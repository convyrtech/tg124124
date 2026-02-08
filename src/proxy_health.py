"""Batch proxy health check with concurrent TCP probing."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Callable

from .database import Database, ProxyRecord

logger = logging.getLogger(__name__)


@dataclass
class ProxyCheckResult:
    """Result of a single proxy health check."""
    proxy_id: int
    host: str
    port: int
    alive: bool
    old_status: str


async def check_proxy_connection(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check if a proxy is reachable via TCP connection.

    Args:
        host: Proxy hostname or IP.
        port: Proxy port.
        timeout: Connection timeout in seconds.

    Returns:
        True if TCP connection succeeded.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def check_proxy_batch(
    db: Database,
    concurrency: int = 50,
    timeout: float = 5.0,
    progress_callback: Optional[Callable[[int, int, ProxyCheckResult], None]] = None,
) -> dict[str, int]:
    """Batch check all proxies in the database for connectivity.

    Args:
        db: Database instance (must be connected).
        concurrency: Max concurrent TCP checks.
        timeout: Timeout per check in seconds.
        progress_callback: Called after each check with (completed, total, result).

    Returns:
        Dict with keys: total, alive, dead, changed.
    """
    proxies = await db.list_proxies()
    if not proxies:
        return {"total": 0, "alive": 0, "dead": 0, "changed": 0}

    sem = asyncio.Semaphore(concurrency)
    counters = {"alive": 0, "dead": 0, "changed": 0, "completed": 0}
    total = len(proxies)

    async def _check_one(proxy: ProxyRecord) -> None:
        async with sem:
            alive = await check_proxy_connection(proxy.host, proxy.port, timeout)

        new_status = "active" if alive else "dead"
        await db.update_proxy(proxy.id, status=new_status)

        if alive:
            counters["alive"] += 1
        else:
            counters["dead"] += 1

        if new_status != proxy.status:
            counters["changed"] += 1

        counters["completed"] += 1

        result = ProxyCheckResult(
            proxy_id=proxy.id,
            host=proxy.host,
            port=proxy.port,
            alive=alive,
            old_status=proxy.status,
        )
        if progress_callback:
            progress_callback(counters["completed"], total, result)

    await asyncio.gather(*[_check_one(p) for p in proxies])

    return {
        "total": total,
        "alive": counters["alive"],
        "dead": counters["dead"],
        "changed": counters["changed"],
    }
