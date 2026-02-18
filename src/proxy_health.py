"""Batch proxy health check with concurrent TCP and SOCKS5+Telegram probing."""

import asyncio
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .database import Database, ProxyRecord

logger = logging.getLogger(__name__)

# Telegram DC2 main endpoint (most reliable for connectivity checks)
_TG_DC2_IP = "149.154.167.50"
_TG_DC2_PORT = 443


@dataclass
class ProxyCheckResult:
    """Result of a single proxy health check."""

    proxy_id: int
    host: str
    port: int
    alive: bool
    old_status: str
    telegram_reachable: bool = False
    error: str | None = None


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
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def check_proxy_telegram(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str | None]:
    """Check if proxy can route traffic to Telegram DC via raw SOCKS5 handshake.

    Does NOT use python_socks (broken on Python 3.13). Implements SOCKS5
    protocol directly: greeting → auth → CONNECT to Telegram DC2.

    Args:
        host: SOCKS5 proxy host.
        port: SOCKS5 proxy port.
        username: Optional SOCKS5 username.
        password: Optional SOCKS5 password.
        timeout: Total timeout in seconds.

    Returns:
        Tuple of (success, error_message). success=True means Telegram is reachable.
    """
    reader = None
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)

        # --- SOCKS5 Greeting ---
        if username and password:
            # Offer username/password ONLY — some proxies close connection
            # if no-auth (0x00) is offered alongside auth methods.
            writer.write(b"\x05\x01\x02")
        else:
            # Offer no-auth only
            writer.write(b"\x05\x01\x00")
        await writer.drain()

        greeting_resp = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if greeting_resp[0] != 0x05:
            return False, f"Not SOCKS5 (version={greeting_resp[0]})"

        chosen_method = greeting_resp[1]

        # --- Username/Password Auth (RFC 1929) ---
        if chosen_method == 0x02:
            if not username or not password:
                return False, "Proxy requires auth but no credentials provided"
            user_bytes = username.encode("utf-8")
            pass_bytes = password.encode("utf-8")
            if len(user_bytes) > 255 or len(pass_bytes) > 255:
                return False, "Username or password exceeds SOCKS5 limit (255 bytes)"
            auth_msg = b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes
            writer.write(auth_msg)
            await writer.drain()

            auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if auth_resp[1] != 0x00:
                return False, "SOCKS5 auth failed (bad credentials)"

        elif chosen_method == 0xFF:
            return False, "Proxy rejected all auth methods"
        elif chosen_method != 0x00:
            return False, f"Unsupported auth method: {chosen_method}"

        # --- CONNECT to Telegram DC2 ---
        target_ip = bytes(int(x) for x in _TG_DC2_IP.split("."))
        connect_msg = (
            b"\x05\x01\x00\x01"  # VER=5, CMD=CONNECT, RSV=0, ATYP=IPv4
            + target_ip
            + struct.pack("!H", _TG_DC2_PORT)
        )
        writer.write(connect_msg)
        await writer.drain()

        # Read SOCKS5 reply (min 10 bytes for IPv4)
        reply = await asyncio.wait_for(reader.readexactly(10), timeout=timeout)

        if reply[0] != 0x05:
            return False, f"Bad SOCKS5 reply version: {reply[0]}"

        reply_code = reply[1]
        if reply_code == 0x00:
            return True, None  # SUCCESS — Telegram DC reachable through proxy

        # Map SOCKS5 error codes
        error_map = {
            0x01: "General failure",
            0x02: "Connection not allowed by ruleset",
            0x03: "Network unreachable",
            0x04: "Host unreachable",
            0x05: "Connection refused",
            0x06: "TTL expired",
            0x07: "Command not supported",
            0x08: "Address type not supported",
        }
        error_msg = error_map.get(reply_code, f"Unknown error code: {reply_code}")
        return False, error_msg

    except TimeoutError:
        return False, "Timeout"
    except ConnectionRefusedError:
        return False, "Connection refused"
    except OSError as e:
        return False, f"Network error: {e}"
    except Exception as e:
        return False, str(e)
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def check_proxy_http(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str | None]:
    """Check if an HTTP proxy can route traffic to Telegram via HTTP CONNECT.

    Sends an HTTP CONNECT request to the proxy targeting Telegram DC2.
    Supports Basic auth via Proxy-Authorization header.

    Args:
        host: HTTP proxy host.
        port: HTTP proxy port.
        username: Optional proxy username.
        password: Optional proxy password.
        timeout: Total timeout in seconds.

    Returns:
        Tuple of (success, error_message). success=True means Telegram is reachable.
    """
    import base64

    reader = None
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )

        # Build HTTP CONNECT request
        target = f"{_TG_DC2_IP}:{_TG_DC2_PORT}"
        request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"

        if username and password:
            creds = base64.b64encode(f"{username}:{password}".encode()).decode()
            request += f"Proxy-Authorization: Basic {creds}\r\n"

        request += "\r\n"
        writer.write(request.encode())
        await writer.drain()

        # Read HTTP response (first line is status)
        response_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        response_str = response_line.decode("utf-8", errors="replace").strip()

        if not response_str:
            return False, "Empty response from proxy"

        # Parse status code: "HTTP/1.1 200 Connection established"
        parts = response_str.split(" ", 2)
        if len(parts) < 2:
            return False, f"Invalid HTTP response: {response_str}"

        try:
            status_code = int(parts[1])
        except ValueError:
            return False, f"Invalid status code: {parts[1]}"

        if status_code == 200:
            return True, None  # Tunnel established — Telegram DC reachable

        reason = parts[2] if len(parts) > 2 else "Unknown"
        if status_code == 407:
            return False, "Proxy auth required (HTTP 407)"
        if status_code == 403:
            return False, "Proxy forbidden (HTTP 403)"
        return False, f"HTTP proxy error: {status_code} {reason}"

    except TimeoutError:
        return False, "Timeout"
    except ConnectionRefusedError:
        return False, "Connection refused"
    except OSError as e:
        return False, f"Network error: {e}"
    except Exception as e:
        return False, str(e)
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def check_proxy_smart(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    protocol: str = "socks5",
    timeout: float = 10.0,
) -> tuple[bool, str | None]:
    """Protocol-aware proxy health check.

    Routes to check_proxy_telegram (SOCKS5) or check_proxy_http (HTTP)
    based on the proxy protocol.

    Args:
        host: Proxy host.
        port: Proxy port.
        username: Optional auth username.
        password: Optional auth password.
        protocol: Proxy protocol ("socks5", "socks4", "http", "https").
        timeout: Timeout in seconds.

    Returns:
        Tuple of (success, error_message).
    """
    if protocol.lower() in ("http", "https"):
        return await check_proxy_http(host, port, username, password, timeout)
    return await check_proxy_telegram(host, port, username, password, timeout)


async def check_proxy_batch(
    db: Database,
    concurrency: int = 50,
    timeout: float = 10.0,
    deep: bool = True,
    progress_callback: Callable[[int, int, ProxyCheckResult], None] | None = None,
) -> dict[str, int]:
    """Batch check all proxies in the database.

    When deep=True (default), tests actual SOCKS5 CONNECT to Telegram DC.
    When deep=False, only checks TCP connectivity (fast but unreliable).

    Args:
        db: Database instance (must be connected).
        concurrency: Max concurrent checks.
        timeout: Timeout per check in seconds.
        deep: If True, test SOCKS5 route to Telegram DC (recommended).
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
            if deep:
                alive, error = await check_proxy_smart(
                    proxy.host,
                    proxy.port,
                    username=proxy.username,
                    password=proxy.password,
                    protocol=proxy.protocol or "socks5",
                    timeout=timeout,
                )
            else:
                alive = await check_proxy_connection(proxy.host, proxy.port, timeout)
                error = None if alive else "TCP unreachable"

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
            telegram_reachable=alive if deep else False,
            error=error,
        )
        if progress_callback:
            progress_callback(counters["completed"], total, result)

    # return_exceptions=True prevents one failed check (e.g. DB error)
    # from aborting all remaining proxy checks.
    results = await asyncio.gather(*[_check_one(p) for p in proxies], return_exceptions=True)

    # Log any exceptions that were silently captured by gather
    errors = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            errors += 1
            if errors <= 5:  # Limit log spam
                logger.warning("Proxy check error for proxy #%d: %s", proxies[i].id, result)
    if errors > 5:
        logger.warning("... and %d more proxy check errors", errors - 5)

    return {
        "total": total,
        "alive": counters["alive"],
        "dead": counters["dead"],
        "changed": counters["changed"],
    }
