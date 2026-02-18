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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import psutil

logger = logging.getLogger(__name__)

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError as e:
    raise ImportError("camoufox not installed. Run: pip install camoufox && camoufox fetch") from e

from .paths import PROFILES_DIR as _PROFILES_DIR
from .proxy_relay import ProxyRelay, needs_relay


@dataclass
class BrowserProfile:
    """Информация о browser профиле"""

    name: str
    path: Path
    proxy: str | None
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


def _get_driver_pid(camoufox_instance) -> int | None:
    """Extract Playwright driver process PID (root of browser process tree).

    This is the node.exe/driver process that communicates with Python via pipe.
    Killing this process cascades to all child processes (firefox/camoufox).

    Args:
        camoufox_instance: AsyncCamoufox context manager instance.

    Returns:
        PID of the driver process, or None if not found.
    """
    try:
        transport = camoufox_instance._connection._transport
        return transport._proc.pid
    except AttributeError:
        logger.debug("Could not extract driver PID: internal API not accessible")
        return None
    except Exception as e:
        logger.warning("Unexpected error extracting driver PID: %s (%s)", e, type(e).__name__)
        return None


def _get_browser_pid(camoufox_instance) -> int | None:
    """Extract PID of camoufox.exe via psutil process tree.

    Chain: camoufox._connection._transport._proc.pid -> node.exe PID
    -> psutil.Process(pid).children() -> camoufox.exe PID

    Args:
        camoufox_instance: AsyncCamoufox context manager instance.

    Returns:
        PID of camoufox.exe (or node.exe as fallback), or None if not found.
    """
    try:
        transport = camoufox_instance._connection._transport
        node_pid = transport._proc.pid
        parent = psutil.Process(node_pid)
        for child in parent.children(recursive=True):
            name = child.name().lower()
            if "camoufox" in name or "firefox" in name:
                return child.pid
        # Fallback: return node_pid (kill node -> cascade kill children)
        return node_pid
    except AttributeError:
        logger.debug("Could not extract browser PID: internal API not accessible")
        return None
    except Exception as e:
        logger.warning("Unexpected error extracting browser PID: %s (%s)", e, type(e).__name__)
        return None


def _clean_session_restore(browser_data_path: Path) -> None:
    """Delete Firefox session restore files that cause launch_persistent_context hangs.

    Firefox stores session state in these files. On subsequent launches,
    Firefox tries to restore the session and hangs before sending the
    "ready" signal to Playwright via Juggler pipe.

    See: https://github.com/microsoft/playwright/issues/12632
    See: https://github.com/microsoft/playwright/issues/12830
    """
    targets = [
        "sessionstore.jsonlz4",
        "sessionstore-backups",
        "sessionCheckpoints.json",
    ]
    for target in targets:
        full_path = browser_data_path / target
        try:
            if full_path.is_dir():
                shutil.rmtree(full_path, ignore_errors=True)
            elif full_path.exists():
                full_path.unlink()
        except OSError:
            pass


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
        self._locks: dict[str, asyncio.Lock] = {}
        self._sync_access_order()

    def _get_lock(self, name: str) -> asyncio.Lock:
        """Get or create a per-profile asyncio.Lock to serialize ensure_active/hibernate."""
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

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

    async def ensure_active(self, name: str, protected: set[str] | None = None) -> Path:
        """Ensure profile is hot (decompressed). Decompress from zip if needed.

        Blocking zip I/O is offloaded to a thread executor so the asyncio
        event loop is not stalled when multiple workers decompress in parallel.

        Uses per-profile lock to prevent race between concurrent ensure_active/hibernate.

        Args:
            name: Profile name.
            protected: Set of profile names that must NOT be evicted.

        Returns:
            Path to the profile directory.
        """
        async with self._get_lock(name):
            profile_path = self.profiles_dir / name
            zip_path = self.profiles_dir / f"{name}.zip"
            # Clean up orphaned .zip.tmp from interrupted hibernate
            tmp_zip = self.profiles_dir / f"{name}.zip.tmp"
            if tmp_zip.exists():
                tmp_zip.unlink(missing_ok=True)
                logger.debug("Cleaned orphaned tmp zip for '%s'", name)

            if self.is_hot(name):
                self._touch(name)
                return profile_path

            if zip_path.exists():
                await self._evict_if_needed(protected)
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._extract_zip, zip_path, profile_path)
                    logger.info("Decompressed cold profile '%s'", name)
                except zipfile.BadZipFile:
                    # FIX #7/#8: Corrupt zip — log, remove zip, and create empty
                    # profile dir so downstream code has a valid path.
                    # The session data is lost but browser will create a fresh profile.
                    logger.exception(
                        "Corrupt zip for profile '%s' — removing and creating fresh profile. "
                        "Previous browser state is lost.",
                        name,
                    )
                    profile_path.mkdir(parents=True, exist_ok=True)
                try:
                    if zip_path.exists():
                        zip_path.unlink()
                except OSError as e:
                    logger.warning("Could not delete zip for '%s': %s", name, e)
                self._touch(name)
                return profile_path

            # New profile — dir will be created later by _build_camoufox_args
            await self._evict_if_needed(protected)
            self._touch(name)
            return profile_path

    @staticmethod
    def _extract_zip(zip_path: Path, dest_path: Path) -> None:
        """Extract zip archive (runs in executor thread).

        Validates all member paths to prevent ZIP Slip (path traversal) attacks.
        """
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Validate paths to prevent ZIP Slip
            dest_str = str(dest_path.resolve())
            for member in zf.namelist():
                member_path = str((dest_path / member).resolve())
                if not member_path.startswith(dest_str):
                    raise ValueError(f"ZIP path traversal detected: {member}")
            zf.extractall(dest_path)

    async def hibernate(self, name: str) -> Path | None:
        """Compress a hot profile to zip and remove the directory.

        Blocking zip I/O is offloaded to a thread executor so the asyncio
        event loop is not stalled when multiple workers compress in parallel.

        Uses atomic write (tmp file + rename) to prevent data loss if
        disk fills during compression.

        Uses per-profile lock to prevent race between concurrent ensure_active/hibernate.

        Args:
            name: Profile name.

        Returns:
            Path to the created zip file, or None if profile was not hot.
        """
        async with self._get_lock(name):
            profile_path = self.profiles_dir / name
            zip_path = self.profiles_dir / f"{name}.zip"
            tmp_zip = self.profiles_dir / f"{name}.zip.tmp"

            if not self.is_hot(name):
                return None

            loop = asyncio.get_running_loop()
            try:
                # FIX: Write to tmp file first, then atomic rename
                await loop.run_in_executor(None, self._compress_zip, profile_path, tmp_zip)
                await loop.run_in_executor(None, tmp_zip.rename, zip_path)
            except Exception:
                # Clean up partial tmp file, keep original directory intact
                if tmp_zip.exists():
                    tmp_zip.unlink(missing_ok=True)
                raise
            await loop.run_in_executor(None, _rmtree_force, profile_path)

            if name in self._access_order:
                self._access_order.remove(name)

            logger.info("Hibernated profile '%s' -> %s", name, zip_path.name)
            return zip_path

    @staticmethod
    def _compress_zip(profile_path: Path, zip_path: Path) -> None:
        """Compress profile directory to zip (runs in executor thread)."""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in profile_path.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(profile_path)
                    zf.write(file_path, arcname)

    def _touch(self, name: str) -> None:
        """Move profile to end of LRU (most recently used)."""
        if name in self._access_order:
            self._access_order.remove(name)
        self._access_order.append(name)

    def _hot_count(self) -> int:
        """Count currently hot profiles using in-memory LRU tracking.

        Uses _access_order length instead of scanning filesystem.
        At 1000 profiles, fs scan is O(1000) per call — too expensive
        when called in a while loop during eviction.
        """
        return len(self._access_order)

    async def _evict_if_needed(self, protected: set[str] | None = None) -> None:
        """Evict LRU profiles until under max_hot capacity."""
        protected = protected or set()
        while self._hot_count() >= self.max_hot:
            evicted = False
            for name in list(self._access_order):
                # Skip protected profiles AND profiles with active locks
                # (another worker may be launching on them right now)
                lock = self._locks.get(name)
                if name not in protected and self.is_hot(name) and (lock is None or not lock.locked()):
                    logger.info("Evicting LRU profile '%s' (capacity %d/%d)", name, self._hot_count(), self.max_hot)
                    await self.hibernate(name)
                    evicted = True
                    break
            if not evicted:
                logger.warning(
                    "Cannot evict: all %d hot profiles are protected. Temporarily exceeding max_hot=%d",
                    self._hot_count(),
                    self.max_hot,
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
                elif entry.suffix == ".zip" and entry.is_file():
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

    PROFILES_DIR = _PROFILES_DIR

    # Hardened Camoufox настройки
    DEFAULT_CONFIG = {
        "geoip": True,  # Авто timezone/locale по IP
        "block_webrtc": True,  # Блокируем WebRTC leak
        "humanize": True,  # Human-like поведение мыши
        "block_images": False,  # Не блокируем картинки (нужны для QR)
        "addons": [],  # Без расширений по умолчанию
    }

    def __init__(self, profiles_dir: Path | None = None):
        self.profiles_dir = profiles_dir or self.PROFILES_DIR
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._active_browsers: dict[str, Any] = {}
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self.lifecycle = ProfileLifecycleManager(self.profiles_dir)

    def _get_profile_lock(self, name: str) -> asyncio.Lock:
        """Get or create per-profile lock to prevent concurrent browser operations."""
        if name not in self._profile_locks:
            self._profile_locks[name] = asyncio.Lock()
        return self._profile_locks[name]

    def get_profile(self, name: str, proxy: str | None = None) -> BrowserProfile:
        """Получает или создаёт профиль"""
        profile_path = self.profiles_dir / name
        profile = BrowserProfile(name=name, path=profile_path, proxy=proxy, created=not profile_path.exists())
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
                        with open(config_path, encoding="utf-8") as f:
                            config = json.load(f)
                            proxy = config.get("proxy")
                    except (OSError, json.JSONDecodeError) as e:
                        logger.warning("Failed to read profile config %s: %s", config_path, e)

                profiles.append(BrowserProfile(name=path.name, path=path, proxy=proxy))
        return profiles

    def _build_camoufox_args(
        self, profile: BrowserProfile, headless: bool = False, extra_args: dict | None = None
    ) -> dict[str, Any]:
        """Собирает аргументы для Camoufox"""
        args = {**self.DEFAULT_CONFIG}
        args["headless"] = headless

        # In frozen mode: use bundled camoufox binary
        if getattr(sys, "frozen", False):
            from .paths import APP_ROOT

            bundled_exe = APP_ROOT / "camoufox" / ("camoufox.exe" if sys.platform == "win32" else "camoufox")
            if bundled_exe.exists():
                args["executable_path"] = str(bundled_exe)

        if profile.proxy:
            args["proxy"] = parse_proxy(profile.proxy)
        else:
            # geoip requires proxy to work — without proxy it hangs
            # trying to determine geolocation via external HTTP request
            args["geoip"] = False

        profile.path.mkdir(parents=True, exist_ok=True)
        args["persistent_context"] = True
        args["user_data_dir"] = str(profile.browser_data_path)

        # Disable Firefox session restore to prevent launch hangs
        # See: https://github.com/microsoft/playwright/issues/12632
        args["firefox_user_prefs"] = {
            "browser.sessionstore.resume_from_crash": False,
            "browser.sessionstore.max_resumed_crashes": 0,
            "browser.sessionstore.max_tabs_undo": 0,
            "browser.sessionstore.max_windows_undo": 0,
            "toolkit.startup.max_resumed_crashes": -1,
        }

        self._save_profile_config(profile)

        if extra_args:
            args.update(extra_args)

        return args

    @staticmethod
    def _mask_proxy_for_config(proxy: str | None) -> str | None:
        """Strip credentials from proxy string for safe storage in profile config.

        Only protocol:host:port is stored. Actual credentials come from
        the database at runtime, not from the profile config file.
        """
        if not proxy:
            return None
        parts = proxy.split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])  # protocol:host:port only
        return proxy

    def _save_profile_config(self, profile: BrowserProfile):
        """Сохраняет конфигурацию профиля"""
        config = {
            "name": profile.name,
            "proxy": self._mask_proxy_for_config(profile.proxy),
        }
        profile.path.mkdir(parents=True, exist_ok=True)
        with open(profile.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    # FIX-007: Timeout для запуска браузера
    BROWSER_LAUNCH_TIMEOUT = 60  # секунд

    @staticmethod
    async def _kill_zombie_browser(camoufox: "AsyncCamoufox") -> None:
        """Kill a Camoufox process that didn't start properly.

        When AsyncCamoufox.__aenter__() times out, the underlying
        camoufox.exe process may still be running. This method attempts
        to close it gracefully, then kills by PID (psutil), falling back
        to taskkill /IM only as last resort.

        IMPORTANT: PID-based kill only affects THIS browser instance,
        NOT other parallel workers' browsers.
        """

        # 1. Try graceful exit via Playwright context manager
        try:
            await asyncio.wait_for(camoufox.__aexit__(None, None, None), timeout=10)
            logger.debug("Zombie browser cleaned up gracefully")
            return
        except Exception as e:
            logger.debug("Graceful cleanup failed: %s", e)

        # 2. PID-based kill (safe for parallel workers)
        pid = _get_browser_pid(camoufox)
        if pid:
            try:
                proc = psutil.Process(pid)
                # Guard against PID reuse: verify process is still a browser
                pname = proc.name().lower()
                if "camoufox" not in pname and "firefox" not in pname:
                    logger.debug("PID %d reused by '%s', skipping kill", pid, proc.name())
                    return
                children = proc.children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                proc.kill()
                logger.info("Killed zombie browser PID %d via psutil", pid)
                await asyncio.sleep(1)
                return
            except psutil.NoSuchProcess:
                logger.debug("Zombie browser PID %d already gone", pid)
                return
            except Exception as e:
                logger.warning("PID-based kill failed for PID %d: %s", pid, e)

        # FIX-E: Do NOT use taskkill /IM — it kills ALL camoufox instances,
        # including other parallel workers' browsers. Accept the zombie as
        # lesser evil; it will be cleaned up on pool shutdown via close_all().
        logger.warning("PID not found for zombie browser cleanup. Process may remain until pool shutdown.")

        # Wait for file locks to release
        await asyncio.sleep(1)

    async def launch(
        self, profile: BrowserProfile, headless: bool = False, extra_args: dict | None = None
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
        # Per-profile lock: prevents two workers from launching same profile
        # simultaneously (e.g., during retry while previous browser is still closing)
        async with self._get_profile_lock(profile.name):
            return await self._launch_impl(profile, headless, extra_args)

    async def _launch_impl(
        self, profile: BrowserProfile, headless: bool = False, extra_args: dict | None = None
    ) -> "BrowserContext":
        """Internal launch implementation, called under profile lock."""
        # Close stale browser if still active for this profile
        if profile.name in self._active_browsers:
            old_ctx = self._active_browsers[profile.name]
            logger.warning("Profile '%s' has stale active browser, closing first", profile.name)
            try:
                await asyncio.wait_for(old_ctx.close(), timeout=20)
            except BaseException as e:
                logger.warning("Force-closing stale browser for '%s': %s", profile.name, e)
                # FIX: Stop zombie relay if browser close failed
                if old_ctx._proxy_relay:
                    try:
                        await old_ctx._proxy_relay.stop()
                    except Exception:
                        pass
                self._active_browsers.pop(profile.name, None)
                # Re-raise CancelledError — outer except BaseException handles cleanup
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise

        # Hot/cold lifecycle: decompress if needed, evict LRU if at capacity
        # Protected set includes active browsers AND the current profile being launched
        protected = set(self._active_browsers.keys()) | {profile.name}
        await self.lifecycle.ensure_active(profile.name, protected=protected)

        proxy_relay = None

        # FIX-003: Очистка stale lock файлов от предыдущего краша
        browser_data_path = profile.browser_data_path
        if browser_data_path.exists():
            # FIX: Delete session restore files that cause Firefox launch hangs
            # https://github.com/microsoft/playwright/issues/12632
            _clean_session_restore(browser_data_path)

            lock_patterns = ["*.lock", "parent.lock", ".parentlock", "lock"]
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
            proxy_info = args.get("proxy", {}).get("server", "no proxy")

        logger.info("Launching Camoufox for '%s'", profile.name)
        logger.info("Profile: %s", profile.browser_data_path)
        logger.info("Proxy: %s", proxy_info)
        logger.info("Headless: %s", headless)

        # FIX-A: Wrap entire launch in try/except to ensure proxy_relay cleanup
        # on ANY exception (not just TimeoutError). Without this, non-timeout
        # errors (OSError, PermissionError, etc.) leave zombie pproxy processes.
        camoufox = AsyncCamoufox(**args)

        try:
            try:
                browser = await asyncio.wait_for(camoufox.__aenter__(), timeout=self.BROWSER_LAUNCH_TIMEOUT)
            except TimeoutError:
                # Kill the zombie Firefox process left by the timed-out launch
                await self._kill_zombie_browser(camoufox)

                # Retry once with a fresh profile (delete corrupted browser_data)
                logger.warning("Browser launch timeout for '%s' — retrying with fresh profile...", profile.name)
                if browser_data_path.exists():
                    try:
                        _rmtree_force(browser_data_path)
                        logger.info("Deleted corrupted browser_data for '%s'", profile.name)
                    except OSError as e:
                        logger.warning("Could not fully delete browser_data for '%s': %s", profile.name, e)

                # Retry: recreate proxy relay (old one may be in broken state)
                if proxy_relay:
                    try:
                        await proxy_relay.stop()
                    except Exception as e:
                        logger.debug("Relay stop on retry: %s", e)

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
                else:
                    # No relay — rebuild args (profile dir will be recreated)
                    args = self._build_camoufox_args(profile, headless, extra_args)

                camoufox = AsyncCamoufox(**args)
                try:
                    browser = await asyncio.wait_for(camoufox.__aenter__(), timeout=self.BROWSER_LAUNCH_TIMEOUT)
                except TimeoutError:
                    await self._kill_zombie_browser(camoufox)
                    if proxy_relay:
                        await proxy_relay.stop()
                        proxy_relay = None  # Prevent double-stop in outer except
                    raise RuntimeError(
                        f"Browser launch timeout after {self.BROWSER_LAUNCH_TIMEOUT}s (retried with fresh profile)"
                    ) from None

            browser_pid = _get_browser_pid(camoufox)
            driver_pid = _get_driver_pid(camoufox)

            ctx = BrowserContext(
                profile=profile,
                browser=browser,
                camoufox=camoufox,
                proxy_relay=proxy_relay,
                manager=self,  # Back-reference for cleanup
            )
            ctx._browser_pid = browser_pid
            ctx._driver_pid = driver_pid

            self._active_browsers[profile.name] = ctx
            return ctx
        except BaseException:
            # Cleanup proxy_relay and camoufox on ANY unhandled exception in launch flow
            # BaseException catches CancelledError (Python 3.11+: not subclass of Exception)
            if "camoufox" in locals() and camoufox:
                if "browser" in locals() and browser:
                    try:
                        await asyncio.wait_for(camoufox.__aexit__(None, None, None), timeout=10)
                    except Exception as cleanup_err:
                        logger.warning("Camoufox cleanup on launch failure: %s", cleanup_err)
                else:
                    # __aenter__ failed or wasn't reached — kill any spawned process
                    try:
                        await self._kill_zombie_browser(camoufox)
                    except Exception as kill_err:
                        logger.warning("Zombie browser kill on launch failure: %s", kill_err)
            if proxy_relay:
                try:
                    await proxy_relay.stop()
                except Exception as relay_err:
                    logger.warning("Relay cleanup error on launch failure: %s", relay_err)
            raise

    async def close_all(self):
        """Закрывает все активные браузеры"""
        _cancelled = None
        for name, ctx in list(self._active_browsers.items()):
            try:
                await ctx.close()
            except BaseException as e:
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)) and _cancelled is None:
                    _cancelled = e
                logger.warning("Error closing %s: %s", name, e)
        self._active_browsers.clear()
        # Prune profile locks to prevent unbounded memory growth over 1000+ accounts
        self._profile_locks.clear()
        if _cancelled is not None:
            raise _cancelled


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
        proxy_relay: ProxyRelay | None = None,
        manager: Optional["BrowserManager"] = None,
    ):
        self.profile = profile
        self.browser = browser
        self._camoufox = camoufox
        self._proxy_relay = proxy_relay
        self._manager = manager
        self._page = None
        self._closed = False
        self._browser_pid: int | None = None
        self._driver_pid: int | None = None
        # FIX #6: Only save storage_state on close when auth succeeded.
        # Callers set this to True after confirmed successful authorization.
        # Default False prevents overwriting valid profiles with failed auth data.
        self.save_state_on_close: bool = False

    async def new_page(self):
        """
        Создаёт или возвращает существующую страницу.

        В persistent context Camoufox открывает браузер с пустой страницей.
        Мы переиспользуем её вместо создания новой (избегаем двух окон).
        """
        await asyncio.sleep(0.5)

        pages = []
        if hasattr(self.browser, "pages"):
            pages = self.browser.pages
        elif hasattr(self.browser, "contexts"):
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
        """Сохраняет storage state (cookies, localStorage) atomically"""
        if self._page and self._page.context:
            state = await self._page.context.storage_state()
            # Atomic write: write to .tmp, then rename to prevent truncated files
            tmp_path = Path(str(self.profile.storage_state_path) + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.profile.storage_state_path)
            logger.info("Storage state saved: %s", self.profile.storage_state_path)

    CLOSE_TIMEOUT = 15  # секунд на закрытие браузера

    def _force_kill_by_pid(self) -> None:
        """Kill browser process tree by PID (psutil). Only kills THIS browser."""
        if not self._browser_pid:
            return
        try:
            proc = psutil.Process(self._browser_pid)
            # Guard against PID reuse: verify process is still a browser
            pname = proc.name().lower()
            if "camoufox" not in pname and "firefox" not in pname:
                logger.debug("PID %d reused by '%s', skipping kill", self._browser_pid, proc.name())
                return
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            proc.kill()
            logger.info("Force-killed browser PID %d for '%s'", self._browser_pid, self.profile.name)
        except psutil.NoSuchProcess:
            logger.debug("Browser PID %d already gone", self._browser_pid)
        except Exception as e:
            logger.warning("Failed to kill browser PID %d: %s", self._browser_pid, e)

    async def close(self):
        """Закрывает браузер и останавливает proxy relay с timeout"""
        if self._closed:
            return
        self._closed = True

        # FIX #6: Only save storage_state if auth was successful.
        # Prevents overwriting valid profiles with failed/partial auth data.
        if self.save_state_on_close:
            try:
                await asyncio.wait_for(self.save_storage_state(), timeout=5)
            except TimeoutError:
                logger.warning("Storage state save timed out for '%s'", self.profile.name)
            except Exception as e:
                logger.warning("Couldn't save state: %s", e)
        else:
            logger.debug("Skipping storage state save for '%s' (save_state_on_close=False)", self.profile.name)

        _cancelled = None
        try:
            await asyncio.wait_for(self._camoufox.__aexit__(None, None, None), timeout=self.CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "Browser close timed out for '%s' - force killing PID %s",
                self.profile.name,
                self._browser_pid,
            )
            self._force_kill_by_pid()
        except BaseException as e:
            logger.warning("Error during browser exit: %s — force killing PID %s", e, self._browser_pid)
            self._force_kill_by_pid()
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                _cancelled = e

        if self._proxy_relay:
            try:
                await self._proxy_relay.stop()
            except BaseException as e:
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)) and _cancelled is None:
                    _cancelled = e
                logger.warning("Error stopping proxy relay: %s", e)

        if self._manager:
            self._manager._active_browsers.pop(self.profile.name, None)

        logger.info("Browser closed for '%s'", self.profile.name)

        if _cancelled is not None:
            raise _cancelled

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc_val, _exc_tb):
        await self.close()


# Utility functions
async def quick_launch(profile_name: str, proxy: str, headless: bool = False) -> BrowserContext:
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
