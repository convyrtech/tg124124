"""
Browser Manager Module
Управление Camoufox браузерами с изоляцией профилей.

Обеспечивает:
- Создание/загрузка persistent профилей
- Конфигурацию прокси с SOCKS5 auth (через proxy relay)
- Hardened fingerprint настройки

ВАЖНО: Браузеры НЕ поддерживают SOCKS5 с авторизацией напрямую.
Используем proxy_relay для создания локального HTTP прокси.
"""
import json
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed. Run: pip install camoufox && camoufox fetch")
    exit(1)

from .proxy_relay import ProxyRelay, needs_relay


@dataclass
class BrowserProfile:
    """Информация о browser профиле"""
    name: str
    path: Path
    proxy: Optional[str]
    created: bool = False

    @property
    def browser_data_path(self) -> Path:
        return self.path / "browser_data"

    @property
    def storage_state_path(self) -> Path:
        return self.path / "storage_state.json"

    @property
    def config_path(self) -> Path:
        return self.path / "profile_config.json"

    def exists(self) -> bool:
        return self.browser_data_path.exists()


def parse_proxy(proxy_str: str) -> Dict[str, Any]:
    """
    Парсит прокси из формата 'socks5:host:port:user:pass'

    Форматы:
    - socks5:host:port:user:pass
    - socks5:host:port
    - http:host:port:user:pass
    """
    parts = proxy_str.split(":")
    if len(parts) == 5:
        proto, host, port, user, pwd = parts
        return {
            "server": f"{proto}://{host}:{port}",
            "username": user,
            "password": pwd,
        }
    elif len(parts) == 3:
        proto, host, port = parts
        return {"server": f"{proto}://{host}:{port}"}
    raise ValueError(f"Invalid proxy format: {proxy_str}. Expected: socks5:host:port:user:pass")


class BrowserManager:
    """
    Менеджер Camoufox браузеров.

    Особенности:
    - Persistent profiles с userDataDir
    - SOCKS5 proxy с auth через Camoufox
    - Auto geoip для timezone/locale
    - WebRTC blocking
    """

    PROFILES_DIR = Path("profiles")

    # Hardened Camoufox настройки
    DEFAULT_CONFIG = {
        "geoip": True,           # Авто timezone/locale по IP
        "block_webrtc": True,    # Блокируем WebRTC leak
        "humanize": True,        # Human-like поведение мыши
        "block_images": False,   # Не блокируем картинки (нужны для QR)
        "addons": [],            # Без расширений по умолчанию
    }

    def __init__(self, profiles_dir: Optional[Path] = None):
        self.profiles_dir = profiles_dir or self.PROFILES_DIR
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._active_browsers: Dict[str, Any] = {}

    def get_profile(self, name: str, proxy: Optional[str] = None) -> BrowserProfile:
        """Получает или создаёт профиль"""
        profile_path = self.profiles_dir / name
        profile = BrowserProfile(
            name=name,
            path=profile_path,
            proxy=proxy,
            created=not profile_path.exists()
        )
        return profile

    def list_profiles(self) -> list[BrowserProfile]:
        """Список всех профилей"""
        profiles = []
        for path in self.profiles_dir.iterdir():
            if path.is_dir() and (path / "browser_data").exists():
                # Читаем конфиг профиля
                config_path = path / "profile_config.json"
                proxy = None
                if config_path.exists():
                    try:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                            proxy = config.get('proxy')
                    except (json.JSONDecodeError, IOError):
                        # Игнорируем битые конфиги
                        pass

                profiles.append(BrowserProfile(
                    name=path.name,
                    path=path,
                    proxy=proxy
                ))
        return profiles

    def _build_camoufox_args(
        self,
        profile: BrowserProfile,
        headless: bool = False,
        extra_args: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Собирает аргументы для Camoufox"""
        args = {**self.DEFAULT_CONFIG}
        args["headless"] = headless

        # Прокси
        if profile.proxy:
            args["proxy"] = parse_proxy(profile.proxy)

        # Persistent context
        profile.path.mkdir(parents=True, exist_ok=True)
        args["persistent_context"] = True
        args["user_data_dir"] = str(profile.browser_data_path)

        # Сохраняем конфиг профиля
        self._save_profile_config(profile)

        # Extra args
        if extra_args:
            args.update(extra_args)

        return args

    def _save_profile_config(self, profile: BrowserProfile):
        """Сохраняет конфигурацию профиля"""
        config = {
            "name": profile.name,
            "proxy": profile.proxy,
        }
        profile.path.mkdir(parents=True, exist_ok=True)
        with open(profile.config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    async def launch(
        self,
        profile: BrowserProfile,
        headless: bool = False,
        extra_args: Optional[Dict] = None
    ) -> "BrowserContext":
        """
        Запускает браузер с профилем.

        ВАЖНО: Если прокси SOCKS5 с авторизацией - автоматически
        запускается локальный proxy relay (браузеры не поддерживают
        SOCKS5 auth напрямую).

        Returns:
            BrowserContext wrapper с page и методами управления
        """
        proxy_relay = None

        # Проверяем нужен ли proxy relay для SOCKS5 с auth
        if profile.proxy and needs_relay(profile.proxy):
            print(f"[BrowserManager] SOCKS5 with auth detected - starting proxy relay...")
            proxy_relay = ProxyRelay(profile.proxy)
            await proxy_relay.start()

            # Создаём временный профиль с локальным HTTP прокси
            relay_profile = BrowserProfile(
                name=profile.name,
                path=profile.path,
                proxy=None  # Прокси передадим через extra_args
            )

            # Передаём локальный HTTP прокси вместо SOCKS5
            if extra_args is None:
                extra_args = {}
            extra_args["proxy"] = proxy_relay.browser_proxy_config

            args = self._build_camoufox_args(relay_profile, headless, extra_args)
            proxy_info = f"{proxy_relay.local_url} -> {profile.proxy.split(':')[1]}:***"
        else:
            args = self._build_camoufox_args(profile, headless, extra_args)
            proxy_info = args.get('proxy', {}).get('server', 'no proxy')

        print(f"[BrowserManager] Launching Camoufox for '{profile.name}'")
        print(f"[BrowserManager] Profile: {profile.browser_data_path}")
        print(f"[BrowserManager] Proxy: {proxy_info}")
        print(f"[BrowserManager] Headless: {headless}")

        camoufox = AsyncCamoufox(**args)
        browser = await camoufox.__aenter__()

        ctx = BrowserContext(
            profile=profile,
            browser=browser,
            camoufox=camoufox,
            proxy_relay=proxy_relay  # Передаём для управления lifecycle
        )

        self._active_browsers[profile.name] = ctx
        return ctx

    async def close_all(self):
        """Закрывает все активные браузеры"""
        for name, ctx in list(self._active_browsers.items()):
            try:
                await ctx.close()
            except Exception as e:
                print(f"[BrowserManager] Warning: error closing {name}: {e}")
        self._active_browsers.clear()


class BrowserContext:
    """
    Контекст браузера с page и управлением.
    Используется как async context manager.
    """

    def __init__(
        self,
        profile: BrowserProfile,
        browser,
        camoufox,
        proxy_relay: Optional[ProxyRelay] = None
    ):
        self.profile = profile
        self.browser = browser
        self._camoufox = camoufox
        self._proxy_relay = proxy_relay  # Для управления lifecycle
        self._page = None
        self._closed = False

    async def new_page(self):
        """
        Создаёт или возвращает существующую страницу.

        В persistent context Camoufox открывает браузер с пустой страницей.
        Мы переиспользуем её вместо создания новой (избегаем двух окон).
        """
        import asyncio

        # Даём время браузеру инициализироваться
        await asyncio.sleep(0.5)

        # Проверяем существующие страницы
        pages = []
        if hasattr(self.browser, 'pages'):
            pages = self.browser.pages
        elif hasattr(self.browser, 'contexts'):
            # Если это Browser, а не BrowserContext - ищем страницы в контекстах
            for ctx in self.browser.contexts:
                pages.extend(ctx.pages)

        if pages:
            print(f"[BrowserContext] Found {len(pages)} existing page(s), reusing first one")
            self._page = pages[0]
            # Закрываем лишние страницы (избегаем двух окон)
            for extra_page in pages[1:]:
                try:
                    await extra_page.close()
                    print(f"[BrowserContext] Closed extra page")
                except Exception as e:
                    print(f"[BrowserContext] Warning: couldn't close extra page: {e}")
            return self._page

        # Создаём новую только если нет существующих
        print(f"[BrowserContext] No existing pages, creating new one")
        self._page = await self.browser.new_page()
        return self._page

    @property
    def page(self):
        """Текущая страница"""
        return self._page

    async def save_storage_state(self):
        """Сохраняет storage state (cookies, localStorage)"""
        if self._page and self._page.context:
            state = await self._page.context.storage_state()
            with open(self.profile.storage_state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
            print(f"[BrowserContext] Storage state saved: {self.profile.storage_state_path}")

    async def close(self):
        """Закрывает браузер и останавливает proxy relay"""
        if self._closed:
            return
        self._closed = True

        try:
            # Сохраняем состояние перед закрытием
            await self.save_storage_state()
        except Exception as e:
            print(f"[BrowserContext] Warning: couldn't save state: {e}")

        try:
            await self._camoufox.__aexit__(None, None, None)
        except Exception as e:
            print(f"[BrowserContext] Warning: error during browser exit: {e}")

        # Останавливаем proxy relay если был запущен
        if self._proxy_relay:
            try:
                await self._proxy_relay.stop()
            except Exception as e:
                print(f"[BrowserContext] Warning: error stopping proxy relay: {e}")

        print(f"[BrowserContext] Browser closed for '{self.profile.name}'")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# Utility functions
async def quick_launch(
    profile_name: str,
    proxy: str,
    headless: bool = False
) -> BrowserContext:
    """
    Быстрый запуск браузера с профилем.

    Usage:
        async with quick_launch("my_account", "socks5:host:port:user:pass") as ctx:
            page = await ctx.new_page()
            await page.goto("https://example.com")
    """
    manager = BrowserManager()
    profile = manager.get_profile(profile_name, proxy)
    return await manager.launch(profile, headless=headless)
