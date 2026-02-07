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
import asyncio
import json
import logging
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError as e:
    raise ImportError("camoufox not installed. Run: pip install camoufox && camoufox fetch") from e

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


# Re-export from utils for backwards compatibility
from .utils import parse_proxy_for_camoufox as parse_proxy


def _on_rmtree_error(func, path, _):
    """Handle read-only files on Windows (Firefox lock/cache files)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree_force(path: Path) -> None:
    """Remove directory tree, handling read-only files (Windows PermissionError)."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rmtree_error)
    else:
        shutil.rmtree(path, onerror=_on_rmtree_error)


class ProfileLifecycleManager:
    """
    Manages hot/cold tiering of browser profiles.

    Hot profiles are decompressed directories ready for browser launch.
    Cold profiles are compressed .zip files saving ~50% disk space.
    LRU eviction keeps at most max_hot profiles decompressed at a time.

    Args:
        profiles_dir: Directory containing all profiles.
        max_hot: Maximum number of decompressed profiles at a time.
    """

    def __init__(self, profiles_dir: Path, max_hot: int = 20):
        self.profiles_dir = profiles_dir
        self.max_hot = max_hot
        self._access_order: list[str] = []
        self._sync_access_order()

    def _sync_access_order(self) -> None:
        """Rebuild LRU access order from filesystem modification times.

        Scans for hot profiles (dirs with browser_data/ subdir) and sorts
        by mtime, oldest first. Called on init to recover from crashes.
        """
        hot_profiles: list[tuple[float, str]] = []
        if not self.profiles_dir.exists():
            return
        for entry in self.profiles_dir.iterdir():
            if entry.is_dir() and (entry / "browser_data").exists():
                mtime = entry.stat().st_mtime
                hot_profiles.append((mtime, entry.name))
        hot_profiles.sort(key=lambda x: x[0])
        self._access_order = [name for _, name in hot_profiles]

    def is_hot(self, name: str) -> bool:
        """Check if profile is decompressed and ready for use."""
        return (self.profiles_dir / name / "browser_data").exists()

    def is_cold(self, name: str) -> bool:
        """Check if profile is compressed as a zip file."""
        return (self.profiles_dir / f"{name}.zip").exists() and not self.is_hot(name)

    def ensure_active(self, name: str, protected: Optional[set[str]] = None) -> Path:
        """Ensure profile is hot (decompressed). Decompress from zip if needed.

        Args:
            name: Profile name.
            protected: Set of profile names that must NOT be evicted.

        Returns:
            Path to the profile directory.
        """
        profile_path = self.profiles_dir / name
        zip_path = self.profiles_dir / f"{name}.zip"

        if self.is_hot(name):
            self._touch(name)
            return profile_path

        if zip_path.exists():
            self._evict_if_needed(protected)
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(profile_path)
                logger.info("Decompressed cold profile '%s'", name)
            except zipfile.BadZipFile:
                logger.error("Corrupt zip for profile '%s', creating fresh profile", name)
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except OSError as e:
                logger.warning("Could not delete zip for '%s': %s", name, e)
            self._touch(name)
            return profile_path

        # New profile — dir will be created later by _build_camoufox_args
        self._evict_if_needed(protected)
        self._touch(name)
        return profile_path

    def hibernate(self, name: str) -> Optional[Path]:
        """Compress a hot profile to zip and remove the directory.

        Args:
            name: Profile name.

        Returns:
            Path to the created zip file, or None if profile was not hot.
        """
        profile_path = self.profiles_dir / name
        zip_path = self.profiles_dir / f"{name}.zip"

        if not self.is_hot(name):
            return None

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path in profile_path.rglob('*'):
                if file_path.is_file():
                    arcname = file_path.relative_to(profile_path)
                    zf.write(file_path, arcname)

        _rmtree_force(profile_path)

        if name in self._access_order:
            self._access_order.remove(name)

        logger.info("Hibernated profile '%s' -> %s", name, zip_path.name)
        return zip_path

    def _touch(self, name: str) -> None:
        """Move profile to end of LRU (most recently used)."""
        if name in self._access_order:
            self._access_order.remove(name)
        self._access_order.append(name)

    def _hot_count(self) -> int:
        """Count currently hot profiles by scanning filesystem."""
        if not self.profiles_dir.exists():
            return 0
        count = 0
        for entry in self.profiles_dir.iterdir():
            if entry.is_dir() and (entry / "browser_data").exists():
                count += 1
        return count

    def _evict_if_needed(self, protected: Optional[set[str]] = None) -> None:
        """Evict LRU profiles until under max_hot capacity."""
        protected = protected or set()
        while self._hot_count() >= self.max_hot:
            evicted = False
            for name in list(self._access_order):
                if name not in protected and self.is_hot(name):
                    logger.info("Evicting LRU profile '%s' (capacity %d/%d)",
                                name, self._hot_count(), self.max_hot)
                    self.hibernate(name)
                    evicted = True
                    break
            if not evicted:
                logger.warning(
                    "Cannot evict: all %d hot profiles are protected. "
                    "Temporarily exceeding max_hot=%d",
                    self._hot_count(), self.max_hot
                )
                break

    def get_stats(self) -> dict:
        """Return profile storage statistics.

        Returns:
            Dict with hot, cold, total, and max_hot counts.
        """
        hot = 0
        cold = 0
        if self.profiles_dir.exists():
            for entry in self.profiles_dir.iterdir():
                if entry.is_dir() and (entry / "browser_data").exists():
                    hot += 1
                elif entry.suffix == '.zip' and entry.is_file():
                    cold += 1
        return {"hot": hot, "cold": cold, "total": hot + cold, "max_hot": self.max_hot}


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
        self.lifecycle = ProfileLifecycleManager(self.profiles_dir)

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
                    except (json.JSONDecodeError, IOError) as e:
                        logger.warning("Failed to read profile config %s: %s", config_path, e)

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

        if profile.proxy:
            args["proxy"] = parse_proxy(profile.proxy)

        profile.path.mkdir(parents=True, exist_ok=True)
        args["persistent_context"] = True
        args["user_data_dir"] = str(profile.browser_data_path)

        self._save_profile_config(profile)

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

    # FIX-007: Timeout для запуска браузера
    BROWSER_LAUNCH_TIMEOUT = 60  # секунд

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

        FIX-003: Удаляет stale lock файлы перед запуском.
        FIX-007: Добавлен timeout на запуск браузера.

        Returns:
            BrowserContext wrapper с page и методами управления
        """
        # Hot/cold lifecycle: decompress if needed, evict LRU if at capacity
        # Protected set includes active browsers AND the current profile being launched
        protected = set(self._active_browsers.keys()) | {profile.name}
        self.lifecycle.ensure_active(profile.name, protected=protected)

        proxy_relay = None

        # FIX-003: Очистка stale lock файлов от предыдущего краша
        browser_data_path = profile.browser_data_path
        if browser_data_path.exists():
            lock_patterns = ['*.lock', 'parent.lock', '.parentlock', 'lock']
            for pattern in lock_patterns:
                for lock_file in browser_data_path.glob(pattern):
                    try:
                        lock_file.unlink()
                        logger.debug("Removed stale lock file: %s", lock_file)
                    except Exception as e:
                        logger.warning("Could not remove lock file %s: %s", lock_file, e)

        if profile.proxy and needs_relay(profile.proxy):
            logger.info("SOCKS5 with auth detected - starting proxy relay...")
            proxy_relay = ProxyRelay(profile.proxy)
            await proxy_relay.start()

            relay_profile = BrowserProfile(
                name=profile.name,
                path=profile.path,
                proxy=None,
            )

            if extra_args is None:
                extra_args = {}
            extra_args["proxy"] = proxy_relay.browser_proxy_config

            args = self._build_camoufox_args(relay_profile, headless, extra_args)
            proxy_info = f"{proxy_relay.local_url} -> {profile.proxy.split(':')[1]}:***"
        else:
            args = self._build_camoufox_args(profile, headless, extra_args)
            proxy_info = args.get('proxy', {}).get('server', 'no proxy')

        logger.info("Launching Camoufox for '%s'", profile.name)
        logger.info("Profile: %s", profile.browser_data_path)
        logger.info("Proxy: %s", proxy_info)
        logger.info("Headless: %s", headless)

        camoufox = AsyncCamoufox(**args)

        try:
            browser = await asyncio.wait_for(
                camoufox.__aenter__(),
                timeout=self.BROWSER_LAUNCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            if proxy_relay:
                await proxy_relay.stop()
            raise RuntimeError(f"Browser launch timeout after {self.BROWSER_LAUNCH_TIMEOUT}s")

        ctx = BrowserContext(
            profile=profile,
            browser=browser,
            camoufox=camoufox,
            proxy_relay=proxy_relay,
            manager=self,  # Back-reference for cleanup
        )

        self._active_browsers[profile.name] = ctx
        return ctx

    async def close_all(self):
        """Закрывает все активные браузеры"""
        for name, ctx in list(self._active_browsers.items()):
            try:
                await ctx.close()
            except Exception as e:
                logger.warning("Error closing %s: %s", name, e)
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
        proxy_relay: Optional[ProxyRelay] = None,
        manager: Optional["BrowserManager"] = None,
    ):
        self.profile = profile
        self.browser = browser
        self._camoufox = camoufox
        self._proxy_relay = proxy_relay
        self._manager = manager
        self._page = None
        self._closed = False

    async def new_page(self):
        """
        Создаёт или возвращает существующую страницу.

        В persistent context Camoufox открывает браузер с пустой страницей.
        Мы переиспользуем её вместо создания новой (избегаем двух окон).
        """
        await asyncio.sleep(0.5)

        pages = []
        if hasattr(self.browser, 'pages'):
            pages = self.browser.pages
        elif hasattr(self.browser, 'contexts'):
            # Если это Browser, а не BrowserContext - ищем страницы в контекстах
            for ctx in self.browser.contexts:
                pages.extend(ctx.pages)

        if pages:
            logger.debug("Found %d existing page(s), reusing first one", len(pages))
            self._page = pages[0]
            for extra_page in pages[1:]:
                try:
                    await extra_page.close()
                    logger.debug("Closed extra page")
                except Exception as e:
                    logger.warning("Couldn't close extra page: %s", e)
            return self._page

        logger.debug("No existing pages, creating new one")
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
            logger.info("Storage state saved: %s", self.profile.storage_state_path)

    CLOSE_TIMEOUT = 15  # секунд на закрытие браузера

    async def close(self):
        """Закрывает браузер и останавливает proxy relay с timeout"""
        if self._closed:
            return
        self._closed = True

        try:
            await asyncio.wait_for(self.save_storage_state(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Storage state save timed out for '%s'", self.profile.name)
        except Exception as e:
            logger.warning("Couldn't save state: %s", e)

        try:
            await asyncio.wait_for(
                self._camoufox.__aexit__(None, None, None),
                timeout=self.CLOSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("Browser close timed out after %ds for '%s'", self.CLOSE_TIMEOUT, self.profile.name)
        except Exception as e:
            logger.warning("Error during browser exit: %s", e)

        if self._proxy_relay:
            try:
                await self._proxy_relay.stop()
            except Exception as e:
                logger.warning("Error stopping proxy relay: %s", e)

        if self._manager and self.profile.name in self._manager._active_browsers:
            del self._manager._active_browsers[self.profile.name]

        logger.info("Browser closed for '%s'", self.profile.name)

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
