"""
Proxy Relay Module
Создаёт локальный HTTP прокси, который перенаправляет трафик через SOCKS5 с авторизацией.

Проблема: Браузеры НЕ поддерживают SOCKS5 proxy с username/password авторизацией.
Решение: Локальный HTTP прокси без auth → внешний SOCKS5 с auth.

Использование:
    relay = ProxyRelay("socks5:host:port:user:pass")
    local_url = await relay.start()  # "http://127.0.0.1:12345"
    # Используем local_url в браузере
    await relay.stop()
"""

import asyncio
import logging
import os
import socket
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    """Конфигурация SOCKS5 прокси"""

    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, proxy_str: str) -> "ProxyConfig":
        """
        Парсит строку прокси.
        Форматы: socks5:host:port:user:pass или socks5:host:port
        """
        parts = proxy_str.split(":")
        if len(parts) == 5:
            proto, host, port, user, pwd = parts
            return cls(proto, host, int(port), user, pwd)
        elif len(parts) == 3:
            proto, host, port = parts
            return cls(proto, host, int(port))
        raise ValueError("Invalid proxy format (expected protocol:host:port[:user:pass])")

    @property
    def has_auth(self) -> bool:
        return bool(self.username and self.password)

    def to_pproxy_uri(self) -> str:
        """Формат для pproxy: socks5://host:port#user:password"""
        if self.has_auth:
            # pproxy использует # для credentials, не @
            return f"{self.protocol}://{self.host}:{self.port}#{self.username}:{self.password}"
        return f"{self.protocol}://{self.host}:{self.port}"


def find_free_port() -> int:
    """Находит свободный порт"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class ProxyRelay:
    """
    Локальный HTTP прокси-relay для SOCKS5 с авторизацией.

    Использует pproxy для создания цепочки:
    Browser → localhost:PORT (HTTP, no auth) → SOCKS5:PORT (with auth)
    """

    def __init__(self, socks5_proxy: str, local_host: str = "127.0.0.1"):
        """
        Args:
            socks5_proxy: Строка прокси в формате socks5:host:port:user:pass
            local_host: Хост для локального прокси (default: 127.0.0.1)
        """
        self.config = ProxyConfig.parse(socks5_proxy)
        self.local_host = local_host
        self.local_port: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._server_handle = None  # For in-process pproxy (frozen exe)
        self._pproxy_task = None  # Monitoring task for in-process pproxy
        self._started = False

    @property
    def local_url(self) -> str | None:
        """URL локального HTTP прокси для браузера"""
        if self.local_port:
            return f"http://{self.local_host}:{self.local_port}"
        return None

    @property
    def browser_proxy_config(self) -> dict | None:
        """Конфиг прокси для Camoufox/Playwright (без auth!)"""
        if self.local_port:
            return {"server": f"http://{self.local_host}:{self.local_port}"}
        return None

    async def start(self) -> str:
        """
        Запускает proxy relay.

        In frozen (PyInstaller) mode: runs pproxy in-process via asyncio.
        In dev mode: spawns pproxy_wrapper.py as subprocess.

        Retries up to 3 times to mitigate TOCTOU race on port allocation.

        Returns:
            URL локального HTTP прокси (http://127.0.0.1:PORT)
        """
        if self._started:
            return self.local_url

        last_error = None
        for attempt in range(3):
            try:
                self.local_port = find_free_port()

                listen_uri = f"http://{self.local_host}:{self.local_port}"
                remote_uri = self.config.to_pproxy_uri()

                logger.info("Starting local HTTP relay...")
                logger.debug("Listen: %s", listen_uri)
                logger.debug("Remote: %s://%s:%s", self.config.protocol, self.config.host, self.config.port)

                if getattr(sys, "frozen", False):
                    await self._start_in_process(listen_uri, remote_uri)
                else:
                    await self._start_subprocess(listen_uri, remote_uri)

                # Health check — verify relay is listening
                for retry in range(10):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                            sock.settimeout(1)
                            sock.connect((self.local_host, self.local_port))
                            logger.debug("Relay verified listening on port %d", self.local_port)
                            break
                    except (TimeoutError, OSError):
                        if retry < 9:
                            await asyncio.sleep(0.3)
                        else:
                            raise RuntimeError(
                                f"Proxy relay not responding on {self.local_host}:{self.local_port}"
                            ) from None

                self._started = True
                logger.info("Started on %s", self.local_url)
                return self.local_url

            except OSError as e:
                last_error = e
                logger.warning("Proxy relay start attempt %d failed (port %s): %s", attempt + 1, self.local_port, e)
                await self._cleanup_on_failure()
                await asyncio.sleep(0.5)
            except Exception:
                await self._cleanup_on_failure()
                raise

        raise RuntimeError(f"Failed to start proxy relay after 3 attempts: {last_error}")

    async def _start_in_process(self, listen_uri: str, remote_uri: str) -> None:
        """Start pproxy relay in-process (for frozen exe — no subprocess needed).

        Wraps pproxy server in an isolated task so crashes don't propagate
        to the main event loop.
        """
        import pproxy

        server = pproxy.Server(listen_uri)
        remote = pproxy.Connection(remote_uri)
        self._server_handle = await server.start_server({"rserver": [remote]})
        # Wrap in a monitored task so exceptions are caught
        self._pproxy_task = asyncio.create_task(self._monitor_pproxy_server())
        logger.debug("ProxyRelay started in-process (frozen mode)")

    async def _monitor_pproxy_server(self) -> None:
        """Monitor in-process pproxy server; log errors without crashing the loop."""
        try:
            # Server handle runs until closed; just await it to catch crashes
            if self._server_handle:
                await self._server_handle.wait_closed()
        except Exception as e:
            logger.exception("In-process pproxy server crashed: %s", e)
            # Don't let it propagate to the event loop

    async def _start_subprocess(self, listen_uri: str, remote_uri: str) -> None:
        """Start pproxy relay as subprocess (dev mode)."""
        wrapper_path = os.path.join(os.path.dirname(__file__), "pproxy_wrapper.py")

        cmd = [sys.executable, wrapper_path, "-l", listen_uri, "-v"]

        # Pass remote URI via environment variable to avoid exposing
        # proxy credentials in process command line (visible in Task Manager)
        env = os.environ.copy()
        env["PPROXY_REMOTE"] = remote_uri

        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )

        await asyncio.sleep(0.5)

        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            await self._process.wait()  # Reap zombie, free transport
            self._process = None
            from .utils import sanitize_error

            raise RuntimeError(f"ProxyRelay failed to start: {sanitize_error(stderr.decode())}")

        logger.debug("ProxyRelay process started (PID: %d)", self._process.pid)

    async def _cleanup_on_failure(self) -> None:
        """Cleanup relay resources after health check failure."""
        if self._pproxy_task and not self._pproxy_task.done():
            self._pproxy_task.cancel()
            self._pproxy_task = None
        if self._server_handle:
            self._server_handle.close()
            self._server_handle = None
        if self._process:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            self._process = None

    async def stop(self):
        """Останавливает proxy relay (subprocess or in-process)."""
        # Cancel monitoring task for in-process pproxy
        if self._pproxy_task and not self._pproxy_task.done():
            self._pproxy_task.cancel()
            self._pproxy_task = None

        # In-process server (frozen mode)
        if self._server_handle:
            try:
                self._server_handle.close()
                await asyncio.wait_for(self._server_handle.wait_closed(), timeout=5)
                logger.debug("ProxyRelay in-process server closed")
            except Exception as e:
                logger.warning("Error closing in-process relay: %s", e)
            finally:
                self._server_handle = None

        # Subprocess (dev mode)
        if self._process:
            pid = self._process.pid
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
                logger.debug("ProxyRelay process terminated (PID: %d)", pid)
            except TimeoutError:
                logger.warning("ProxyRelay process did not terminate in 5s, killing (PID: %d)", pid)
                self._process.kill()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except TimeoutError:
                    logger.exception("ProxyRelay process stuck after kill (PID: %d)", pid)
            except ProcessLookupError:
                logger.debug("Process already terminated (PID: %d)", pid)
            finally:
                self._process = None

        self._started = False
        logger.info("Stopped")

    async def __aenter__(self) -> "ProxyRelay":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()


class ProxyRelayManager:
    """
    Менеджер для нескольких proxy relay (для batch операций).
    """

    def __init__(self):
        self._relays: dict[str, ProxyRelay] = {}

    async def get_or_create(self, socks5_proxy: str) -> ProxyRelay:
        """
        Получает существующий relay или создаёт новый.
        Кэширует по proxy строке для переиспользования.

        FIX #12: Check if cached relay is still running (may have been
        stopped by stop_all()). If stopped, create a fresh one.
        """
        existing = self._relays.get(socks5_proxy)
        if existing and existing._started:
            return existing

        # Create new relay (replace stopped one in cache)
        relay = ProxyRelay(socks5_proxy)
        await relay.start()
        self._relays[socks5_proxy] = relay

        return relay

    async def stop_all(self):
        """Останавливает все relay"""
        for relay in self._relays.values():
            await relay.stop()
        self._relays.clear()

    async def __aenter__(self) -> "ProxyRelayManager":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop_all()


def needs_relay(proxy_str: str) -> bool:
    """
    Проверяет, нужен ли proxy relay для данного прокси.
    Нужен если: SOCKS5 + username/password.

    Firefox/Camoufox НЕ поддерживает SOCKS5 с аутентификацией напрямую,
    поэтому нужен локальный HTTP relay.
    """
    if not proxy_str:
        return False

    config = ProxyConfig.parse(proxy_str)
    return config.protocol.lower() in ("socks5", "socks4") and config.has_auth


async def test_relay():
    """Тестирование proxy relay"""
    import sys

    if len(sys.argv) < 2:
        logger.info("Usage: python -m src.proxy_relay socks5:host:port:user:pass")
        return

    proxy = sys.argv[1]
    # Mask credentials in log output
    proxy_parts = proxy.split(":")
    if len(proxy_parts) == 5:
        masked = f"{proxy_parts[0]}:{proxy_parts[1]}:{proxy_parts[2]}:***:***"
    else:
        masked = proxy
    logger.info("Testing proxy: %s", masked)
    logger.info("Needs relay: %s", needs_relay(proxy))

    if needs_relay(proxy):
        async with ProxyRelay(proxy) as relay:
            logger.info("Local proxy: %s", relay.local_url)
            logger.info("Browser config: %s", relay.browser_proxy_config)

            # Держим открытым для теста
            logger.info("Relay running. Press Ctrl+C to stop...")
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass
    else:
        logger.info("Proxy doesn't need relay (no auth or HTTP)")


if __name__ == "__main__":
    asyncio.run(test_relay())
