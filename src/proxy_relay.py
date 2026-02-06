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
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    """Конфигурация SOCKS5 прокси"""
    protocol: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

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
        raise ValueError(f"Invalid proxy format: {proxy_str}")

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
        s.bind(('127.0.0.1', 0))
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
        self.local_port: Optional[int] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started = False

    @property
    def local_url(self) -> Optional[str]:
        """URL локального HTTP прокси для браузера"""
        if self.local_port:
            return f"http://{self.local_host}:{self.local_port}"
        return None

    @property
    def browser_proxy_config(self) -> Optional[dict]:
        """Конфиг прокси для Camoufox/Playwright (без auth!)"""
        if self.local_port:
            return {"server": f"http://{self.local_host}:{self.local_port}"}
        return None

    async def start(self) -> str:
        """
        Запускает proxy relay.

        Returns:
            URL локального HTTP прокси (http://127.0.0.1:PORT)
        """
        if self._started:
            return self.local_url

        self.local_port = find_free_port()

        listen_uri = f"http://{self.local_host}:{self.local_port}"
        remote_uri = self.config.to_pproxy_uri()

        wrapper_path = os.path.join(os.path.dirname(__file__), "pproxy_wrapper.py")

        cmd = [
            "python", wrapper_path,
            "-l", listen_uri,
            "-r", remote_uri,
            "-v"  # Verbose для отладки
        ]

        logger.info("Starting local HTTP relay...")
        logger.debug("Listen: %s", listen_uri)
        logger.debug("Remote: %s://%s:%s", self.config.protocol, self.config.host, self.config.port)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        await asyncio.sleep(0.5)

        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            raise RuntimeError(f"ProxyRelay failed to start: {stderr.decode()}")

        logger.debug("ProxyRelay process started (PID: %d)", self._process.pid)

        # FIX-011: Health check - проверяем что relay слушает на порту
        for retry in range(10):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    sock.connect((self.local_host, self.local_port))
                    logger.debug("Relay verified listening on port %d", self.local_port)
                    break
            except (socket.error, socket.timeout):
                if retry < 9:
                    await asyncio.sleep(0.3)
                else:
                    raise RuntimeError(
                        f"Proxy relay not responding on {self.local_host}:{self.local_port}"
                    )

        self._started = True
        logger.info("Started on %s", self.local_url)

        return self.local_url

    async def stop(self):
        """Останавливает proxy relay"""
        if self._process:
            pid = self._process.pid
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
                logger.debug("ProxyRelay process terminated (PID: %d)", pid)
            except asyncio.TimeoutError:
                logger.warning("ProxyRelay process did not terminate in 5s, killing (PID: %d)", pid)
                self._process.kill()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    logger.error("ProxyRelay process stuck after kill (PID: %d)", pid)
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
        """
        if socks5_proxy not in self._relays:
            relay = ProxyRelay(socks5_proxy)
            await relay.start()
            self._relays[socks5_proxy] = relay

        return self._relays[socks5_proxy]

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
    return config.protocol.lower() in ('socks5', 'socks4') and config.has_auth


async def test_relay():
    """Тестирование proxy relay"""
    import sys

    if len(sys.argv) < 2:
        logger.info("Usage: python -m src.proxy_relay socks5:host:port:user:pass")
        return

    proxy = sys.argv[1]
    # Mask credentials in log output
    masked = proxy.split(":")
    if len(masked) == 5:
        masked = f"{masked[0]}:{masked[1]}:{masked[2]}:***:***"
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
