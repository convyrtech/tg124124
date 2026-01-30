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
import socket
from typing import Optional, Tuple
from dataclasses import dataclass


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

        # pproxy команда:
        # -l http://127.0.0.1:PORT  - слушаем HTTP на локальном порту
        # -r socks5://user:pass@host:port  - перенаправляем на SOCKS5
        listen_uri = f"http://{self.local_host}:{self.local_port}"
        remote_uri = self.config.to_pproxy_uri()

        # Запускаем pproxy как subprocess
        cmd = [
            "python", "-m", "pproxy",
            "-l", listen_uri,
            "-r", remote_uri,
            "-v"  # Verbose для отладки
        ]

        print(f"[ProxyRelay] Starting local HTTP relay...")
        print(f"[ProxyRelay] Listen: {listen_uri}")
        print(f"[ProxyRelay] Remote: {self.config.protocol}://{self.config.host}:{self.config.port}")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Ждём запуска
        await asyncio.sleep(1)

        # Проверяем что процесс запустился
        if self._process.returncode is not None:
            stderr = await self._process.stderr.read()
            raise RuntimeError(f"ProxyRelay failed to start: {stderr.decode()}")

        self._started = True
        print(f"[ProxyRelay] Started on {self.local_url}")

        return self.local_url

    async def stop(self):
        """Останавливает proxy relay"""
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            except ProcessLookupError:
                pass  # Процесс уже завершён

            self._process = None

        self._started = False
        print(f"[ProxyRelay] Stopped")

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
    """
    if not proxy_str:
        return False

    config = ProxyConfig.parse(proxy_str)
    # Браузеры не поддерживают SOCKS5 с auth
    return config.protocol.lower() in ('socks5', 'socks4') and config.has_auth


# Тест
async def test_relay():
    """Тестирование proxy relay"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.proxy_relay socks5:host:port:user:pass")
        return

    proxy = sys.argv[1]
    print(f"Testing proxy: {proxy}")
    print(f"Needs relay: {needs_relay(proxy)}")

    if needs_relay(proxy):
        async with ProxyRelay(proxy) as relay:
            print(f"Local proxy: {relay.local_url}")
            print(f"Browser config: {relay.browser_proxy_config}")

            # Держим открытым для теста
            print("\nRelay running. Press Ctrl+C to stop...")
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass
    else:
        print("Proxy doesn't need relay (no auth or HTTP)")


if __name__ == "__main__":
    asyncio.run(test_relay())
