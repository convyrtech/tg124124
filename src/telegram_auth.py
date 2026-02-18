"""
Telegram Web Authorization Module
QR-код авторизация web.telegram.org через существующую Telethon сессию.

Принцип работы:
1. Браузер открывает web.telegram.org → появляется QR
2. Извлекаем token из QR (screenshot → pyzbar decode)
3. Telethon client вызывает auth.acceptLoginToken
4. Браузер получает авторизацию
5. Обрабатываем 2FA если требуется

ВАЖНО: НЕ логировать auth_key или api_hash!
"""

import asyncio
import base64
import io
import json
import logging
import math
import random
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .paths import PROFILES_DIR
from typing import TYPE_CHECKING, Any, Optional

# Logger for this module
logger = logging.getLogger(__name__)

# Suppress noisy Telethon internal logs
logging.getLogger("telethon").setLevel(logging.ERROR)

# QR decoding - use OpenCV (works on Windows without extra DLLs)
try:
    import cv2
    import numpy as np
    from PIL import Image
except ImportError as e:
    raise ImportError("opencv-python/Pillow not installed. Run: pip install opencv-python Pillow") from e

# pyzbar is optional - lazy load to avoid DLL errors on Windows
pyzbar = None


def _get_pyzbar():
    global pyzbar
    if pyzbar is None:
        try:
            from pyzbar import pyzbar as _pyzbar

            pyzbar = _pyzbar
        except (ImportError, OSError, FileNotFoundError):
            logger.warning("pyzbar not available (DLL not found or import error), will use OpenCV only")
    return pyzbar


# Telethon
try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.account import SetAuthorizationTTLRequest
    from telethon.tl.functions.auth import AcceptLoginTokenRequest
except ImportError as e:
    raise ImportError("telethon not installed. Run: pip install telethon") from e

from .browser_manager import BrowserManager
from .utils import sanitize_error

if TYPE_CHECKING:
    from .resource_monitor import ResourceMonitor


# Watchdog timeout for browser operations (seconds).
# Kills browser process tree via threading.Timer if authorize() hangs.
# Independent of asyncio event loop — works even when Playwright pipe I/O
# blocks the Windows ProactorEventLoop (observed with Camoufox headless).
BROWSER_WATCHDOG_TIMEOUT = 240  # 4 minutes


class BrowserWatchdog:
    """Thread-based watchdog that kills browser process tree on timeout.

    When page.goto() hangs in Camoufox headless on Windows, the Playwright
    pipe I/O can block the asyncio event loop, preventing asyncio.timeout()
    from firing. This watchdog uses threading.Timer (OS-level, independent
    of asyncio) to kill the browser after BROWSER_WATCHDOG_TIMEOUT seconds.

    Usage:
        watchdog = BrowserWatchdog(driver_pid, browser_pid, "account_name")
        watchdog.start()
        try:
            await page.goto(...)
        finally:
            watchdog.cancel()
    """

    def __init__(
        self,
        driver_pid: int | None,
        browser_pid: int | None,
        profile_name: str,
        timeout: float = BROWSER_WATCHDOG_TIMEOUT,
    ):
        self._driver_pid = driver_pid
        self._browser_pid = browser_pid
        self._profile_name = profile_name
        self._timeout = timeout
        self._timer = threading.Timer(timeout, self._kill)
        self._timer.daemon = True

    def start(self) -> None:
        """Start the watchdog timer."""
        self._timer.start()

    def cancel(self) -> None:
        """Cancel the watchdog (call in finally block)."""
        self._timer.cancel()

    def _kill(self) -> None:
        """Kill entire browser process tree. Runs in timer thread."""
        import psutil

        logger.warning(
            "Watchdog timeout (%ds) for '%s' — killing browser process tree (driver PID %s, browser PID %s)",
            self._timeout,
            self._profile_name,
            self._driver_pid,
            self._browser_pid,
        )

        # Kill browser first, then driver (driver holds the pipe to Python)
        for pid in [self._browser_pid, self._driver_pid]:
            if not pid:
                continue
            try:
                proc = psutil.Process(pid)
                children = proc.children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                proc.kill()
                logger.info(
                    "Watchdog killed PID %d for '%s'",
                    pid,
                    self._profile_name,
                )
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                logger.warning(
                    "Watchdog kill failed for PID %d: %s",
                    pid,
                    e,
                )


@dataclass
class DeviceConfig:
    """Конфигурация устройства для синхронизации Telethon/Browser"""

    device_model: str = "Desktop"
    system_version: str = "Windows 10"
    app_version: str = "5.5.2 x64"
    lang_code: str = "en"
    system_lang_code: str = "en-US"

    @property
    def os_type(self) -> str:
        """Определяет тип ОС из system_version"""
        sv = self.system_version.lower()
        if "windows" in sv:
            return "windows"
        elif "mac" in sv or "darwin" in sv:
            return "macos"
        elif "linux" in sv or "ubuntu" in sv:
            return "linux"
        return "windows"  # Default

    @property
    def browser_os_list(self) -> list:
        """Возвращает список ОС для Camoufox (только одна для консистентности)"""
        return [self.os_type]


@dataclass
class AccountConfig:
    """Конфигурация аккаунта из файлов"""

    name: str
    session_path: Path
    api_id: int
    api_hash: str
    proxy: str | None = None
    phone: str | None = None
    device: DeviceConfig = field(default_factory=DeviceConfig)

    @classmethod
    def load(cls, account_dir: Path) -> "AccountConfig":
        """
        Загружает конфиг аккаунта из директории.

        Args:
            account_dir: Путь к директории аккаунта

        Returns:
            AccountConfig с загруженными данными

        Raises:
            FileNotFoundError: Если session или api.json не найдены
            json.JSONDecodeError: Если JSON файлы невалидны
            KeyError: Если отсутствуют обязательные поля в api.json
        """
        # Ищем session файл
        session_files = list(account_dir.glob("*.session"))
        if not session_files:
            raise FileNotFoundError(f"No .session file in {account_dir}")
        session_path = session_files[0]

        # Читаем api.json
        api_path = account_dir / "api.json"
        if not api_path.exists():
            raise FileNotFoundError(f"api.json not found in {account_dir}")

        try:
            with open(api_path, encoding="utf-8") as f:
                api_config = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(f"Invalid JSON in {api_path}: {e.msg}", e.doc, e.pos) from e

        # Normalize field names: support app_id/app_hash aliases
        _FIELD_ALIASES = {
            "api_id": ["app_id", "API_ID", "appId"],
            "api_hash": ["app_hash", "API_HASH", "appHash"],
        }
        for canonical, aliases in _FIELD_ALIASES.items():
            if canonical not in api_config:
                for alias in aliases:
                    if alias in api_config:
                        api_config[canonical] = api_config[alias]
                        break

        # Проверяем обязательные поля
        if "api_id" not in api_config:
            raise KeyError(
                f"'api_id' not found in {api_path}. "
                f"Expected 'api_id' (or app_id/appId). Found {len(api_config)} keys."
            )
        if "api_hash" not in api_config:
            raise KeyError(
                f"'api_hash' not found in {api_path}. "
                f"Expected 'api_hash' (or app_hash/appHash). Found {len(api_config)} keys."
            )

        # Ensure api_id is int (some configs store it as string)
        try:
            api_config["api_id"] = int(api_config["api_id"])
        except (ValueError, TypeError):
            raise ValueError(
                f"'api_id' must be integer in {api_path}, got: {type(api_config['api_id']).__name__}"
            )

        # Извлекаем device конфигурацию (FIX #3: DEVICE SYNC)
        device = DeviceConfig(
            device_model=api_config.get("device_model", api_config.get("device", "Desktop")),
            system_version=api_config.get("system_version", "Windows 10"),
            app_version=api_config.get("app_version", "5.5.2 x64"),
            lang_code=api_config.get("lang_code", "en"),
            system_lang_code=api_config.get("system_lang_code", "en-US"),
        )

        # Читаем ___config.json (опционально)
        proxy = None
        name = account_dir.name
        config_path = account_dir / "___config.json"
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                    proxy = config.get("Proxy")
                    name = config.get("Name", name)
            except json.JSONDecodeError as e:
                # Config опциональный, но логируем ошибку
                logger.exception(f"[AccountConfig] Invalid JSON in {config_path}: {e.msg} at line {e.lineno}")

        return cls(
            name=name,
            session_path=session_path,
            api_id=api_config["api_id"],
            api_hash=api_config["api_hash"],
            proxy=proxy,
            device=device,
        )


class ErrorCategory:
    """FIX-4.1: Error categories for batch error breakdown."""

    DEAD_SESSION = "dead_session"
    BAD_PROXY = "bad_proxy"
    QR_DECODE_FAIL = "qr_decode_fail"
    TWO_FA_REQUIRED = "2fa_required"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    BROWSER_CRASH = "browser_crash"
    UNKNOWN = "unknown"


def classify_error(error_message: str) -> str:
    """FIX-4.1: Classify error message into an ErrorCategory.

    Args:
        error_message: Error string from AuthResult

    Returns:
        ErrorCategory string constant
    """
    if not error_message:
        return ErrorCategory.UNKNOWN

    msg = error_message.lower()

    if any(s in msg for s in ("not authorized", "auth_key_unregistered", "session is not authorized", "dead session")):
        return ErrorCategory.DEAD_SESSION
    if any(
        s in msg
        for s in ("connectionerror", "proxy error", "proxy connection", "connection refused", "socks", "proxy failed")
    ):
        return ErrorCategory.BAD_PROXY
    if any(s in msg for s in ("failed to extract qr", "qr not found", "qr_decode", "qr token")):
        return ErrorCategory.QR_DECODE_FAIL
    if any(s in msg for s in ("2fa", "password required", "two-factor", "incorrect password")):
        return ErrorCategory.TWO_FA_REQUIRED
    if any(s in msg for s in ("floodwaiterror", "flood wait", "too many attempts", "rate limit")):
        return ErrorCategory.RATE_LIMITED
    if any(s in msg for s in ("timeout", "timed out")):
        return ErrorCategory.TIMEOUT
    if any(s in msg for s in ("browser crash", "target closed", "connection closed", "browser target")):
        return ErrorCategory.BROWSER_CRASH

    return ErrorCategory.UNKNOWN


@dataclass
class AuthResult:
    """Результат авторизации"""

    success: bool
    profile_name: str
    error: str | None = None
    error_category: str = ""
    required_2fa: bool = False
    user_info: dict[str, Any] | None = None
    telethon_alive: bool = False  # FIX #2: Session safety check

    def __post_init__(self):
        """Auto-classify error if not set."""
        if self.error and not self.error_category:
            self.error_category = classify_error(self.error)


def decode_qr_from_screenshot(screenshot_bytes: bytes) -> bytes | None:
    """
    Декодирует QR-код из скриншота.

    Args:
        screenshot_bytes: PNG скриншот в bytes

    Returns:
        Token bytes или None если QR не найден/не декодирован
    """
    from PIL import ImageEnhance, ImageOps

    image = Image.open(io.BytesIO(screenshot_bytes))

    def extract_token(data):
        """Extract token bytes from tg://login URL"""
        if not data or "tg://login?token=" not in data:
            return None
        token_b64 = data.split("token=")[1]
        if "&" in token_b64:
            token_b64 = token_b64.split("&")[0]
        padding = 4 - len(token_b64) % 4
        if padding != 4:
            token_b64 += "=" * padding
        return base64.urlsafe_b64decode(token_b64)

    # Try zxing-cpp with morphological preprocessing (handles Telegram dot-style QR)
    try:
        import zxingcpp

        def _zxing_decode_morph(pil_img):
            """Decode QR via zxing-cpp with scale + morph close for dot-style QR."""
            # 1. Try raw image
            results = zxingcpp.read_barcodes(pil_img)
            for r in results:
                if r.text and "tg://login?token=" in r.text:
                    return r.text

            # 2. Scale up + binarize + morphological close
            # Two presets: high-contrast (dots) and low-contrast (thin lines)
            img_gray = np.array(pil_img.convert("L"))
            h, w = img_gray.shape
            scaled = cv2.resize(img_gray, (w * 4, h * 4), interpolation=cv2.INTER_NEAREST)

            for threshold in (128, 80):
                _, binary = cv2.threshold(scaled, threshold, 255, cv2.THRESH_BINARY)
                for ksize in (9, 13, 17):
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
                    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
                    results = zxingcpp.read_barcodes(Image.fromarray(closed))
                    for r in results:
                        if r.text and "tg://login?token=" in r.text:
                            return r.text
            return None

        def _get_qr_crops(pil_img):
            """Get candidate QR crop regions for full-page screenshots."""
            w, h = pil_img.size
            crops = []

            # 1. Contour-based detection (multiple thresholds)
            gray_np = np.array(pil_img.convert("L"))
            for thr in (150, 120, 100):
                _, thresh = cv2.threshold(gray_np, thr, 255, cv2.THRESH_BINARY)
                contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
                squares = []
                for c in contours:
                    bx, by, bw, bh = cv2.boundingRect(c)
                    area = cv2.contourArea(c)
                    if 100 < area < 50000 and bw > 10 and 0.6 <= bh / bw <= 1.6:
                        squares.append((bx, by, bw, bh))

                if len(squares) >= 3:
                    xs = [s[0] for s in squares]
                    ys = [s[1] for s in squares]
                    x_max = max(s[0] + s[2] for s in squares)
                    y_max = max(s[1] + s[3] for s in squares)
                    span = max(x_max - min(xs), y_max - min(ys))
                    # Only use if region is roughly square (QR-shaped)
                    if span > 0 and 0.5 <= (x_max - min(xs)) / (y_max - min(ys)) <= 2.0:
                        pad = int(span * 0.15)
                        region = (
                            max(0, min(xs) - pad),
                            max(0, min(ys) - pad),
                            min(w, x_max + pad),
                            min(h, y_max + pad),
                        )
                        crops.append(region)
                        break  # Use first successful threshold

            # 2. Telegram Web K typical QR positions
            # FIX-1.3: Center crop first (auth QR appears centered on page)
            crops.append((int(w * 0.25), int(h * 0.1), int(w * 0.75), int(h * 0.65)))
            # Right-side crops as fallback
            crops.append((int(w * 0.5), 0, w, int(h * 0.55)))
            crops.append((int(w * 0.52), int(h * 0.05), int(w * 0.88), int(h * 0.52)))

            return crops

        # Try direct decode on full image
        data = _zxing_decode_morph(image)
        if data:
            token_bytes = extract_token(data)
            if token_bytes:
                logger.info("Decoded QR with zxing-cpp (morphological)")
                return token_bytes

        # For large images: try multiple crop strategies
        if image.width > 500 or image.height > 500:
            for region in _get_qr_crops(image):
                qr_crop = image.crop(region)
                data = _zxing_decode_morph(qr_crop)
                if data:
                    token_bytes = extract_token(data)
                    if token_bytes:
                        logger.info("Decoded QR with zxing-cpp (cropped + morphological)")
                        return token_bytes
    except ImportError:
        logger.debug("zxing-cpp not available, skipping")
    except Exception as e:
        logger.debug("zxing-cpp error: %s", e)

    # Try QRCodeDetectorAruco (more robust than standard detector)
    try:
        aruco_detector = cv2.QRCodeDetectorAruco()
        img_np = np.array(image.convert("RGB"))
        cv_img = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        decoded, _, _ = aruco_detector.detectAndDecode(cv_img)
        if decoded and decoded[0]:
            for data in decoded:
                if data and "tg://login?token=" in data:
                    token_bytes = extract_token(data)
                    if token_bytes:
                        logger.info("Decoded QR with QRCodeDetectorAruco")
                        return token_bytes
    except Exception as e:
        logger.debug(f"QRCodeDetectorAruco error: {e}")

    # Конвертируем PIL Image в numpy array для OpenCV
    def pil_to_cv2(pil_img):
        """Convert PIL Image to OpenCV format"""
        if pil_img.mode == "1":
            pil_img = pil_img.convert("L")
        if pil_img.mode == "L":
            return np.array(pil_img)
        elif pil_img.mode == "RGB":
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        elif pil_img.mode == "RGBA":
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGBA2BGR)
        return np.array(pil_img.convert("RGB"))

    def decode_with_opencv(cv_img):
        """Try to decode QR with OpenCV"""
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(cv_img)
        return data if data else None

    def decode_with_pyzbar(pil_img):
        """Try to decode QR with pyzbar if available"""
        pyzbar_module = _get_pyzbar()
        if pyzbar_module is None:
            return None
        try:
            decoded = pyzbar_module.decode(pil_img)
            for obj in decoded:
                return obj.data.decode("utf-8")
        except Exception as e:
            logger.warning(f"[QR] pyzbar decode error: {type(e).__name__}: {e}")
        return None

    # Пробуем декодировать в разных вариантах с OpenCV/pyzbar
    # Telegram Web использует белый QR на тёмном фоне - нужно инвертировать
    variants = []

    # 1. Оригинал
    variants.append(("original", image))

    # 2. Grayscale
    gray = image.convert("L")
    variants.append(("grayscale", gray))

    # 3. Инвертированный RGB (для белого QR на тёмном)
    try:
        inverted_rgb = ImageOps.invert(image.convert("RGB"))
        variants.append(("inverted_rgb", inverted_rgb))
    except Exception as e:
        logger.debug(f"[QR] inverted_rgb variant failed: {e}")

    # 4. Инвертированный grayscale
    try:
        inverted_gray = ImageOps.invert(gray)
        variants.append(("inverted_gray", inverted_gray))
    except Exception as e:
        logger.debug(f"[QR] inverted_gray variant failed: {e}")

    # 5. Высококонтрастная версия
    try:
        enhancer = ImageEnhance.Contrast(gray)
        high_contrast = enhancer.enhance(2.0)
        variants.append(("high_contrast", high_contrast))
        variants.append(("high_contrast_inv", ImageOps.invert(high_contrast)))
    except Exception as e:
        logger.debug(f"[QR] high_contrast variant failed: {e}")

    # 6. Thresholding (бинаризация)
    try:
        threshold = gray.point(lambda x: 255 if x > 128 else 0, "L")
        variants.append(("threshold", threshold))
        threshold_inv = gray.point(lambda x: 0 if x > 128 else 255, "L")
        variants.append(("threshold_inv", threshold_inv))
    except Exception as e:
        logger.debug(f"[QR] threshold variant failed: {e}")

    for name, img_variant in variants:
        # Try OpenCV first (more reliable on Windows)
        try:
            cv_img = pil_to_cv2(img_variant)
            data = decode_with_opencv(cv_img)
            if data and "tg://login?token=" in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    logger.info(f"Decoded QR with OpenCV using {name}")
                    return token_bytes
        except Exception as e:
            logger.debug(f"[QR] OpenCV decode failed for {name}: {e}")

        # Fallback to pyzbar if available
        try:
            data = decode_with_pyzbar(img_variant)
            if data and "tg://login?token=" in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    logger.info(f"Decoded QR with pyzbar using {name}")
                    return token_bytes
        except Exception as e:
            logger.debug(f"[QR] pyzbar decode failed for {name}: {e}")

    return None


def extract_token_from_tg_url(url_str: str) -> bytes | None:
    """
    Извлекает token bytes из tg://login URL.

    Args:
        url_str: URL в формате tg://login?token=BASE64TOKEN

    Returns:
        Token bytes или None
    """
    if not url_str or "tg://login?token=" not in url_str:
        return None

    try:
        token_b64 = url_str.split("token=")[1]
        # Удаляем возможные лишние параметры
        if "&" in token_b64:
            token_b64 = token_b64.split("&")[0]
        # Добавляем padding
        padding = 4 - len(token_b64) % 4
        if padding != 4:
            token_b64 += "=" * padding
        return base64.urlsafe_b64decode(token_b64)
    except Exception as e:
        logger.debug("Error extracting token from URL: %s", type(e).__name__)
        return None


def parse_telethon_proxy(proxy_str: str) -> tuple | None:
    """
    Конвертирует прокси в формат Telethon.
    Input: socks5:host:port:user:pass (password may contain colons)
    Output: (socks.SOCKS5, host, port, True, user, pass)
    """
    if not proxy_str:
        return None

    import socks

    # Split with maxsplit=4 to handle passwords containing colons
    parts = proxy_str.split(":", 4)

    def _resolve_proxy_type(proto_str: str) -> int:
        """Map protocol string to PySocks constant."""
        p = proto_str.lower()
        if "socks5" in p:
            return socks.SOCKS5
        if "socks4" in p:
            return socks.SOCKS4
        # http, https, or anything else → HTTP CONNECT
        return socks.HTTP

    try:
        if len(parts) == 5:
            proto, host, port, user, pwd = parts
            return (_resolve_proxy_type(proto), host, int(port), True, user, pwd)
        elif len(parts) == 4:
            proto, host, port, user = parts
            return (_resolve_proxy_type(proto), host, int(port), True, user, "")
        elif len(parts) == 3:
            proto, host, port = parts
            return (_resolve_proxy_type(proto), host, int(port))
    except (ValueError, AttributeError):
        return None

    return None


class TelegramAuth:
    """
    Основной класс для QR-авторизации Telegram Web.
    """

    TELEGRAM_WEB_URL = "https://web.telegram.org/k/"
    QR_WAIT_TIMEOUT = 30  # секунд ждать появления QR
    AUTH_WAIT_TIMEOUT = 120  # секунд ждать завершения авторизации
    QR_MAX_RETRIES = 8  # FIX-6.1: Increased from 5 to 8 for 1000-account reliability
    QR_RETRY_DELAY = 5  # секунд между retry

    def __init__(
        self,
        account: AccountConfig,
        browser_manager: BrowserManager | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.account = account
        self.browser_manager = browser_manager or BrowserManager()
        self._client: TelegramClient | None = None
        self._on_status = on_status

    def _status(self, msg: str) -> None:
        """Report progress to callback (GUI) and logger."""
        logger.info(msg)
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    @staticmethod
    async def _safe_disconnect(client: TelegramClient) -> None:
        """Disconnect client ignoring errors (for cleanup on connect failure)."""
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            pass

    async def _create_telethon_client(self) -> TelegramClient:
        """
        Создаёт Telethon client из существующей сессии с синхронизированным device.

        FIX-002: Включает WAL режим для SQLite чтобы избежать "database locked".
        FIX-006: Добавлен timeout на connect().
        """
        proxy = parse_telethon_proxy(self.account.proxy)
        device = self.account.device

        # FIX-002: Включить WAL режим для SQLite session перед открытием
        # Это позволяет параллельное чтение и уменьшает блокировки
        # Offloaded to executor: sqlite3.connect(timeout=10) can block up to 10s
        session_path = self.account.session_path
        if session_path.exists():

            def _enable_wal(path: str) -> None:
                c = sqlite3.connect(path, timeout=10)
                try:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA busy_timeout=10000")
                finally:
                    c.close()

            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _enable_wal, str(session_path))
                logger.debug("SQLite WAL mode enabled for session")
            except sqlite3.Error as e:
                logger.warning("Could not set WAL mode for session: %s", e)

        # FIX #3: DEVICE SYNC - передаём device параметры
        try:
            client = TelegramClient(
                str(self.account.session_path.with_suffix("")),  # Без .session
                self.account.api_id,
                self.account.api_hash,
                proxy=proxy,
                device_model=device.device_model,
                system_version=device.system_version,
                app_version=device.app_version,
                lang_code=device.lang_code,
                system_lang_code=device.system_lang_code,
                auto_reconnect=False,
                connection_retries=0,
                receive_updates=False,
            )
        except sqlite3.DatabaseError as e:
            raise RuntimeError(f"Session file corrupted: {e}") from e

        # FIX-006: Timeout на connect() чтобы не зависать навечно
        # FIX-4.4: Catch TypeError from broken proxy libs + ConnectionError
        # Improved diagnostics: include proxy info (host:port only, no credentials)
        proxy_info = ""
        if proxy and isinstance(proxy, list | tuple) and len(proxy) >= 3:
            proxy_info = f" [proxy: {proxy[1]}:{proxy[2]}]"
            # Warn if port looks like HTTP but used as SOCKS5
            _HTTP_PORTS = {80, 8080, 8888, 3128, 3129}
            if proxy[2] in _HTTP_PORTS:
                logger.warning(
                    "Proxy %s:%s uses port %s which is typically HTTP, not SOCKS5. "
                    "This may cause authentication failures.",
                    proxy[1], proxy[2], proxy[2],
                )
        elif not proxy:
            proxy_info = " [no proxy]"

        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except sqlite3.DatabaseError as e:
            await self._safe_disconnect(client)
            raise RuntimeError(f"Session file corrupted: {e}") from e
        except TimeoutError:
            await self._safe_disconnect(client)
            raise RuntimeError(f"Telethon connect timeout after 30s{proxy_info}") from None
        except TypeError as e:
            await self._safe_disconnect(client)
            raise RuntimeError(f"Telethon connection failed (proxy lib error): {e}{proxy_info}") from e
        except (ConnectionError, OSError) as e:
            await self._safe_disconnect(client)
            error_msg = str(e)
            if "0 time(s)" in error_msg:
                # connection_retries=0 means no retries — add clarity
                error_msg = f"Cannot connect to Telegram (check proxy/network){proxy_info}"
            else:
                error_msg = f"Telethon connection failed: {e}{proxy_info}"
            raise RuntimeError(error_msg) from e

        try:
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
                await asyncio.wait_for(client.disconnect(), timeout=5)
                raise RuntimeError(
                    f"Session is not authorized (expired or revoked){proxy_info}. Re-login or get a fresh .session file."
                )

            # Получаем инфо о текущем пользователе (без логирования sensitive data)
            me = await asyncio.wait_for(client.get_me(), timeout=15)
            logger.info(f"Connected as: {me.first_name} (ID: {me.id})")
            logger.debug(f"Device: {device.device_model} / {device.system_version}")
        except RuntimeError:
            raise  # Already handled above (not authorized)
        except Exception as e:
            await self._safe_disconnect(client)
            raise RuntimeError(f"Telethon post-connect check failed: {e}{proxy_info}") from e

        return client

    async def _verify_telethon_session(self, client: TelegramClient) -> bool:
        """
        FIX #2: Проверяет что Telethon сессия всё ещё работает после авторизации браузера.
        """
        try:
            me = await asyncio.wait_for(client.get_me(), timeout=15)
            if me:
                logger.info(f"Session verified: {me.first_name} still authorized")
                return True
        except Exception as e:
            logger.warning(f"Session verification failed: {e}")
        return False

    AUTH_TTL_DAYS = 365  # Maximum authorization lifetime

    async def _set_authorization_ttl(self, client: TelegramClient) -> bool:
        """
        Set authorization TTL to maximum (365 days) for all sessions.

        This extends the web session lifetime so it doesn't auto-expire.
        Non-fatal: failure here doesn't affect the migration result.

        Returns:
            True if TTL was set successfully
        """
        try:
            result = await asyncio.wait_for(
                client(SetAuthorizationTTLRequest(authorization_ttl_days=self.AUTH_TTL_DAYS)),
                timeout=15,
            )
            logger.info("Authorization TTL set to %d days", self.AUTH_TTL_DAYS)
            return bool(result)
        except Exception as e:
            logger.warning("Failed to set authorization TTL: %s", e)
            return False

    def _is_profile_already_authorized(self, profile) -> bool:
        """Check if profile's storage_state.json already has user_auth.

        This allows skipping browser launch for already-migrated accounts
        during retry-failed runs (where authorize() was killed by watchdog
        after storage_state was saved but before returning success).
        """
        storage_state_path = profile.path / "storage_state.json"
        if not storage_state_path.exists():
            return False
        try:
            with open(storage_state_path, encoding="utf-8") as f:
                state = json.load(f)
            for origin in state.get("origins", []):
                if origin.get("origin") != "https://web.telegram.org":
                    continue
                for item in origin.get("localStorage", []):
                    if item.get("name") == "user_auth":
                        user_auth = json.loads(item["value"])
                        if user_auth.get("id"):
                            # Validate auth age — reject if older than TTL
                            import time as _time

                            auth_date = user_auth.get("date")
                            if auth_date:
                                try:
                                    age_days = (_time.time() - int(auth_date)) / 86400
                                    if age_days > self.AUTH_TTL_DAYS:
                                        logger.info(
                                            "Pre-check: auth expired (%.0f days old, TTL=%d)",
                                            age_days,
                                            self.AUTH_TTL_DAYS,
                                        )
                                        return False
                                except (ValueError, TypeError):
                                    pass  # Can't parse date, treat as valid
                            logger.info(
                                "Pre-check: storage_state.json has user_auth (date=%s)",
                                user_auth.get("date"),
                            )
                            return True
            return False
        except Exception as e:
            logger.debug("Pre-check storage_state.json failed: %s", e)
            return False

    async def _check_page_state(self, page) -> str:
        """
        Определяет текущее состояние страницы Telegram Web.

        Returns:
            "qr_login" - на странице QR кода для логина
            "2fa_required" - требуется ввод 2FA пароля
            "authorized" - уже авторизован, в чате
            "loading" - страница ещё загружается
            "unknown" - неизвестное состояние
        """
        try:
            # Quick browser alive check — detect dead browser immediately
            # instead of falling through all selectors with silent exceptions.
            try:
                await page.evaluate("1")
            except Exception as e:
                err = str(e).lower()
                if any(p in err for p in ("target closed", "connection closed", "has been closed", "browser has been")):
                    logger.debug("Browser dead: %s", e)
                    return "dead"
                # Other errors (e.g. execution context) — continue with DOM checks

            # Проверяем URL
            current_url = page.url
            logger.debug(f"Checking page state, URL: {current_url}")

            # FIX-010: Улучшенная детекция "authorized" состояния
            # Проверяем признаки авторизованного состояния:
            # 1. Наличие chat items (li с peer-id)
            # 2. Наличие sidebar/columns
            # 3. Наличие avatar/user info
            # 4. URL patterns

            # Метод 1: Проверяем наличие chat items (самый надёжный индикатор)
            chat_items = await page.query_selector(
                "[data-peer-id], "  # Chat items with peer ID
                ".chatlist-chat, "  # Individual chats
                "li.chatlist-chat, "  # Chat list items
                ".dialog, "  # Dialog elements
                '[class*="ListItem"][class*="Chat"]'  # Generic chat list items
            )
            if chat_items:
                try:
                    is_visible = await chat_items.is_visible()
                    if is_visible:
                        logger.debug("Authorized: found visible chat items")
                        return "authorized"
                except Exception:
                    # Element exists - likely authorized
                    logger.debug("Authorized: chat items found (visibility check failed)")
                    return "authorized"

            # Метод 2: Проверяем структуру страницы (columns/sidebar)
            columns = await page.query_selector(
                ".tabs-tab, "  # Tab navigation
                ".sidebar, "  # Sidebar
                "#column-left, "  # Left column
                ".chats-container, "  # Chats container
                ".folders-tabs, "  # Folder tabs
                '[class*="LeftColumn"], '  # Left column variations
                '[class*="ChatFolders"]'  # Chat folders
            )
            if columns:
                try:
                    is_visible = await columns.is_visible()
                    if is_visible:
                        logger.debug("Authorized: found visible columns/sidebar")
                        return "authorized"
                except Exception:
                    pass

            # Метод 3: Проверяем наличие аватара пользователя в шапке (признак авторизации)
            user_avatar = await page.query_selector(
                ".avatar-like-icon, "  # User avatar
                '[class*="Avatar"], '  # Avatar component
                ".profile-photo, "  # Profile photo
                ".menu-toggle"  # Menu toggle (only in authorized state)
            )
            if user_avatar:
                try:
                    is_visible = await user_avatar.is_visible()
                    if is_visible:
                        # Дополнительно проверяем что это не аватар в QR login
                        qr_container = await page.query_selector('.auth-image, [class*="qr"]')
                        if not qr_container:
                            logger.debug("Authorized: found visible user avatar")
                            return "authorized"
                except Exception:
                    pass

            # Метод 4: Проверяем URL pattern (после авторизации URL меняется)
            # /k/#@ или /a/#@ - указывает на конкретный чат
            # /k/# без дополнительных параметров после # может быть и login и main page
            if ("@" in current_url) or ("/k/#-" in current_url) or ("/a/#-" in current_url):
                logger.debug(f"Authorized: URL pattern indicates chat view: {current_url}")
                return "authorized"

            # Метод 5: Проверяем через JavaScript наличие активной сессии
            try:
                has_session = await page.evaluate("""
                    () => {
                        // Check if app is initialized and has user
                        try {
                            // Telegram Web K stores user in various places
                            if (window.App && window.App.managers && window.App.managers.appUsersManager) {
                                const self = window.App.managers.appUsersManager.getSelf();
                                if (self && self.id) return true;
                            }
                            // Check localStorage for auth state
                            const authState = localStorage.getItem('authState') || localStorage.getItem('auth_state');
                            if (authState && authState.includes('"userId"')) return true;
                            // Check for user_auth in IDB (indirect check via DOM)
                            const chatList = document.querySelector('[data-peer-id]');
                            if (chatList) return true;
                        } catch (e) {}
                        return false;
                    }
                """)
                if has_session:
                    logger.debug("Authorized: JavaScript check confirmed session")
                    return "authorized"
            except Exception as e:
                logger.debug(f"JS session check error: {e}")

            # ВАЖНО: Проверяем 2FA форму РАНЬШЕ чем QR (на странице 2FA тоже может быть canvas)
            password_input = await page.query_selector('input[type="password"]')
            if password_input:
                try:
                    is_visible = await password_input.is_visible()
                    if is_visible:
                        logger.debug("2FA required: password input visible")
                        return "2fa_required"
                except Exception:
                    pass

            # Проверяем текст "Enter Your Password" на странице
            try:
                page_text = await page.inner_text("body")
                if "Enter Your Password" in page_text or "Two-Step Verification" in page_text:
                    logger.debug("2FA required: password text found")
                    return "2fa_required"
            except Exception:
                pass

            # Проверяем QR код (только если это не 2FA страница и не authorized)
            qr_canvas = await page.query_selector("canvas")
            if qr_canvas:
                try:
                    is_visible = await qr_canvas.is_visible()
                    if is_visible:
                        # Дополнительно проверяем что это QR login page по тексту
                        try:
                            qr_text = await page.inner_text("body")
                            # QR login page has specific text
                            qr_indicators = ["scan", "qr", "log in", "phone", "quick"]
                            if any(ind in qr_text.lower() for ind in qr_indicators):
                                logger.debug("QR login: canvas and login text found")
                                return "qr_login"
                        except Exception:
                            pass
                        # Canvas visible but no clear login text - might still be login
                        logger.debug("QR login: canvas visible (no clear text)")
                        return "qr_login"
                except Exception:
                    pass

            # Проверяем индикатор загрузки
            spinner = await page.query_selector('[class*="spinner"], [class*="loading"], [class*="preloader"]')
            if spinner:
                try:
                    is_visible = await spinner.is_visible()
                    if is_visible:
                        logger.debug("Loading: spinner visible")
                        return "loading"
                except Exception:
                    pass

            logger.debug("Unknown page state")
            return "unknown"

        except Exception as e:
            error_msg = str(e).encode("ascii", "replace").decode("ascii")
            # Detect dead browser from any unhandled Playwright exception
            err_lower = error_msg.lower()
            if any(
                p in err_lower for p in ("target closed", "connection closed", "has been closed", "browser has been")
            ):
                logger.debug(f"Browser dead (from exception): {error_msg}")
                return "dead"
            logger.debug(f"Error checking page state: {error_msg}")
            return "unknown"

    async def _wait_for_qr(self, page, timeout: int = QR_WAIT_TIMEOUT) -> bytes | None:
        """
        Ждёт появления QR-кода и извлекает токен.

        Использует несколько методов:
        1. JS extraction - напрямую из DOM/переменных страницы
        2. Canvas screenshot + external decoding (zxing-cpp / OpenCV / pyzbar)

        Returns:
            Token bytes или screenshot bytes (для дальнейшего декодирования)
        """
        logger.info(f"Waiting for QR code (timeout: {timeout}s)...")

        for attempt in range(timeout):
            try:
                # Метод 0: Попробуем получить QR token из памяти приложения
                qr_token_direct = await page.evaluate("""
                    () => {
                        // Telegram Web K stores QR token in various places
                        try {
                            // Method 1: Check MTProto state
                            if (window.MTProto && window.MTProto.qrToken) {
                                return 'tg://login?token=' + btoa(String.fromCharCode(...window.MTProto.qrToken));
                            }

                            // Method 2: Check for exportLoginToken result in App state
                            if (window.App && window.App.managers) {
                                const managers = window.App.managers;
                                if (managers.authState && managers.authState.qrToken) {
                                    return managers.authState.qrToken;
                                }
                            }

                            // Method 3: Look in sessionStorage/localStorage
                            for (const storage of [sessionStorage, localStorage]) {
                                for (let i = 0; i < storage.length; i++) {
                                    const key = storage.key(i);
                                    const val = storage.getItem(key);
                                    if (val && val.includes('tg://login?token=')) {
                                        const match = val.match(/tg:\\/\\/login\\?token=[A-Za-z0-9_-]+/);
                                        if (match) return match[0];
                                    }
                                }
                            }

                            // Method 4: Check IndexedDB for auth state
                            // (async, can't do here easily)

                        } catch (e) {}
                        return null;
                    }
                """)

                if qr_token_direct and "tg://login?token=" in str(qr_token_direct):
                    logger.info("Token found directly in app state")
                    token_bytes = extract_token_from_tg_url(qr_token_direct)
                    if token_bytes:
                        return token_bytes

                # Получить canvas data для внешнего декодирования (zxing-cpp / OpenCV / pyzbar)
                # FIX-1.1: toDataURL() works on both 2d AND WebGL canvases
                # without needing getContext('2d') — which returns null on WebGL
                qr_from_canvas = await page.evaluate("""
                    () => {
                        const canvas = document.querySelector('canvas');
                        if (!canvas) return null;

                        try {
                            return {
                                dataUrl: canvas.toDataURL('image/png'),
                                width: canvas.width,
                                height: canvas.height
                            };
                        } catch(e) { return null; }
                    }
                """)

                if qr_from_canvas and qr_from_canvas.get("dataUrl"):
                    # Decode the canvas data URL externally
                    try:
                        canvas_data = qr_from_canvas["dataUrl"].split(",")[1]
                        canvas_bytes = base64.b64decode(canvas_data)

                        # Try to decode this smaller canvas image
                        token = decode_qr_from_screenshot(canvas_bytes)
                        if token:
                            logger.info("Token decoded from canvas (external)")
                            return token
                    except Exception as e:
                        logger.debug(f"Canvas decode error: {e}")

                # Метод 1: Комплексное JS извлечение токена
                token_from_js = await page.evaluate("""
                    () => {
                        // 1. Ищем ссылки с tg://login
                        const links = document.querySelectorAll('a[href*="tg://login"]');
                        for (let link of links) {
                            const href = link.href || link.getAttribute('href');
                            if (href && href.includes('token=')) {
                                return href;
                            }
                        }

                        // 2. Ищем в data атрибутах
                        const qrElements = document.querySelectorAll('[data-qr], [data-token]');
                        for (let el of qrElements) {
                            if (el.dataset.qr) return el.dataset.qr;
                            if (el.dataset.token) return 'tg://login?token=' + el.dataset.token;
                        }

                        // 3. Ищем скрытые элементы с QR данными
                        const allElements = document.querySelectorAll('[class*="qr"], [id*="qr"]');
                        for (let el of allElements) {
                            const text = el.textContent || el.innerText || '';
                            if (text.includes('tg://login?token=')) {
                                const match = text.match(/tg:\\/\\/login\\?token=[A-Za-z0-9_-]+/);
                                if (match) return match[0];
                            }
                        }

                        // 4. Пробуем найти в глобальных переменных (Telegram Web K)
                        try {
                            if (window.App && window.App.qrToken) {
                                return 'tg://login?token=' + window.App.qrToken;
                            }
                        } catch (e) {}

                        // 5. Ищем SVG QR код с xlink:href
                        const svgLinks = document.querySelectorAll('svg a, a svg');
                        for (let el of svgLinks) {
                            const parent = el.closest('a') || el.querySelector('a');
                            if (parent) {
                                const href = parent.href || parent.getAttribute('href') || parent.getAttribute('xlink:href');
                                if (href && href.includes('tg://login')) {
                                    return href;
                                }
                            }
                        }

                        return null;
                    }
                """)

                if token_from_js and "tg://login?token=" in str(token_from_js):
                    logger.info("Token found via JS extraction")
                    # Извлекаем token bytes напрямую
                    token_bytes = extract_token_from_tg_url(token_from_js)
                    if token_bytes:
                        return token_bytes

                # Метод 2: Ищем QR canvas и делаем скриншот элемента
                qr_element = await page.query_selector("canvas")

                if qr_element:
                    # Check bounding box — skip if canvas is hidden or zero-sized
                    bbox = await qr_element.bounding_box()
                    if not bbox or bbox["width"] < 10 or bbox["height"] < 10:
                        continue  # Canvas not visible yet, retry next iteration

                    # Ждём полной отрисовки QR
                    await asyncio.sleep(2)

                    # FIX-1.2: Screenshot of canvas element only, not full page
                    # Full page = 1280x720, QR = ~200x200 = 3% pixels → decoders fail
                    try:
                        screenshot = await qr_element.screenshot()
                    except Exception:
                        # Element screenshot failed (hidden/detached) — skip this iteration
                        continue
                    return screenshot

            except Exception as e:
                # Detect dead browser immediately instead of waiting for timeout
                err_str = str(e).lower()
                if any(
                    p in err_str
                    for p in ("target closed", "connection closed", "has been closed", "browser has been closed")
                ):
                    logger.warning("Browser died during QR wait")
                    return None
                # Ignore selector errors, keep retrying
                if attempt == timeout - 1:
                    logger.warning(f"QR search error: {e}")

            await asyncio.sleep(1)

            if attempt % 5 == 0 and attempt > 0:
                logger.debug(f"Still waiting for QR... ({attempt}s)")

            # FIX-2.1: Re-check page state every 10s to detect 2FA/authorized mid-wait
            if attempt > 0 and attempt % 10 == 0:
                try:
                    state = await self._check_page_state(page)
                    if state in ("2fa_required", "authorized"):
                        logger.info(f"Page state changed to '{state}' during QR wait, aborting")
                        return None
                except Exception:
                    pass

        return None

    def _is_screenshot_bytes(self, data: bytes) -> bool:
        """
        FIX-001: Проверяет является ли data изображением (screenshot).

        Проверяет magic bytes для PNG, JPEG, GIF форматов.
        """
        if len(data) < 8:
            return False

        # PNG magic: 89 50 4E 47 0D 0A 1A 0A
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return True

        # JPEG magic: FF D8 FF
        if data[:3] == b"\xff\xd8\xff":
            return True

        # GIF magic: GIF87a or GIF89a
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return True

        return False

    def _is_tg_url_token(self, data: bytes) -> bool:
        """
        FIX-001: Проверяет является ли data tg://login?token=... URL.
        """
        try:
            text = data.decode("utf-8", errors="strict")
            if "tg://login?token=" in text:
                token_part = text.split("token=")[1].split("&")[0]
                if 20 <= len(token_part) <= 100:
                    return True
            return False
        except (UnicodeDecodeError, IndexError):
            return False

    async def _extract_qr_token_with_retry(self, page) -> bytes | None:
        """
        FIX #4: QR RETRY - извлекает QR токен с повторными попытками.

        Поддерживает два варианта ответа от _wait_for_qr:
        1. Token bytes - если JS успешно извлёк токен (tg://login?token=...)
        2. Screenshot bytes - для декодирования через pyzbar

        FIX-001: Используем проверку формата вместо размера для определения типа.
        """
        # Clean up old debug screenshots (keep only last 10)
        try:
            debug_dir = PROFILES_DIR
            for pattern in ["debug_qr_*.png", "debug_2fa_*.png"]:
                files = sorted(debug_dir.glob(pattern), key=lambda f: f.stat().st_mtime)
                for f in files[:-10]:  # Keep last 10
                    f.unlink(missing_ok=True)
        except Exception:
            pass  # Non-critical cleanup

        profile_name = self.account.name.replace(" ", "_").replace("/", "_")

        for retry in range(self.QR_MAX_RETRIES):
            if retry > 0:
                logger.info(f"QR retry {retry + 1}/{self.QR_MAX_RETRIES}...")
                # Обновляем страницу для нового QR
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.warning(f"Page reload warning: {e}")

                # FIX-2.2: Check page state after reload (may show 2FA, error, already-authorized)
                await asyncio.sleep(3)
                try:
                    post_reload_state = await self._check_page_state(page)
                    if post_reload_state == "2fa_required":
                        logger.warning("Page shows 2FA after reload, stopping QR extraction")
                        return None
                    if post_reload_state == "authorized":
                        logger.info("Already authorized after reload")
                        return None
                except Exception:
                    pass

                # FIX-6.2: Exponential backoff between retries (capped at 25s to stay within QR token lifetime)
                delay = min(self.QR_RETRY_DELAY * (1.5**retry), 25)
                await asyncio.sleep(delay)

            result = await self._wait_for_qr(page)

            if not result:
                logger.warning(f"QR not found on page (attempt {retry + 1})")
                continue

            # FIX-001: Определяем тип данных по содержимому
            # 1. Если это tg:// URL - извлекаем token
            # 2. Если это изображение (PNG/JPEG) - декодируем QR
            # 3. Иначе - это уже raw token bytes

            if self._is_tg_url_token(result):
                # tg://login?token=... URL - извлекаем token bytes
                token = extract_token_from_tg_url(result.decode("utf-8"))
                if token:
                    logger.info(f"Token extracted from URL ({len(token)} bytes)")
                    return token

            elif self._is_screenshot_bytes(result):
                # Это screenshot, декодируем QR
                token = decode_qr_from_screenshot(result)
                if token:
                    logger.info(f"Token extracted from screenshot ({len(token)} bytes)")
                    return token
                else:
                    logger.warning(f"Failed to decode QR from screenshot (attempt {retry + 1})")
                    # FIX-009: Сохраняем debug скриншот с timestamp
                    timestamp = datetime.now().strftime("%H%M%S")
                    debug_path = PROFILES_DIR / f"debug_qr_{profile_name}_{timestamp}_r{retry}.png"
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(debug_path, "wb") as f:
                        f.write(result)
                    logger.info(f"Debug screenshot saved: {debug_path}")

            else:
                # Это уже raw token bytes (например, после canvas decode)
                logger.info(f"Token already decoded ({len(result)} bytes)")
                return result

        return None

    async def _accept_token(self, client: TelegramClient, token: bytes) -> tuple[bool, str | None]:
        """
        Отправляет acceptLoginToken для авторизации браузера с FloodWaitError handling.

        Returns:
            Tuple (success, error_reason). error_reason is None on success.

        Реализует:
        - FloodWaitError handling: returns False so outer loop fetches fresh QR
          (QR tokens expire in ~30s, so retrying with stale token after flood wait is useless)
        - Non-retryable error detection (EXPIRED, INVALID, ALREADY_ACCEPTED)
        - Exponential backoff для других ошибок
        - Максимум 3 попытки
        """
        max_retries = 3
        base_delay = 5

        for attempt in range(max_retries):
            try:
                logger.info(f"Accepting login token (attempt {attempt + 1}/{max_retries})...")
                result = await client(AcceptLoginTokenRequest(token=token))
                logger.info(f"Token accepted! Authorization: {type(result).__name__}")
                return True, None

            except FloodWaitError as e:
                wait_time = e.seconds
                logger.warning(
                    "FloodWaitError: must wait %ds — token is now stale, need fresh QR (attempt %d/%d)",
                    wait_time,
                    attempt + 1,
                    max_retries,
                )
                # QR tokens expire in ~30s. After any FloodWait, the token is
                # definitely stale. Return False so the outer retry loop
                # (_extract_qr_token_with_retry) fetches a fresh QR code.
                return False, f"FloodWait: Telegram requires waiting {wait_time}s before next attempt"

            except Exception as e:
                error_str = str(e).upper()
                # FIX #7: Don't retry stale/invalid tokens — let outer loop
                # re-fetch a fresh QR instead of wasting time + FloodWait risk
                non_retryable = ("EXPIRED", "ALREADY_ACCEPTED", "INVALID")
                if any(keyword in error_str for keyword in non_retryable):
                    logger.warning("Token error (non-retryable): %s — need fresh QR", e)
                    return False, f"Token error: {e}"

                # Exponential backoff для других ошибок
                delay = base_delay * (2**attempt) + random.uniform(0, 3)
                logger.warning(
                    f"Error accepting token: {e}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )

                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)

        logger.error("Failed to accept token after all retries")
        return False, "Failed to accept token after all retries"

    async def _wait_for_auth_complete(self, page, timeout: int = AUTH_WAIT_TIMEOUT) -> tuple[bool, bool]:
        """
        Ждёт завершения авторизации в браузере.
        Returns: (success, required_2fa)
        """
        logger.info(f"Waiting for browser authorization (timeout: {timeout}s)...")

        for i in range(timeout):
            # Dead browser detection — avoid looping 120s on a crashed browser
            try:
                await page.evaluate("1")
            except Exception as e:
                err = str(e).lower()
                if any(p in err for p in ("target closed", "connection closed", "has been closed")):
                    logger.warning("Browser dead during auth completion wait")
                    return (False, False)

            current_url = page.url

            # Проверяем успешную авторизацию - ищем элементы главной страницы
            chat_list = await page.query_selector('.chatlist, .chat-list, [class*="ChatList"], .folders-tabs')
            search_input = await page.query_selector('input[placeholder="Search"], .input-search')

            if chat_list or search_input:
                logger.info("Authorization successful (chat list found)")
                return (True, False)

            # Также проверяем URL
            if "/k/#" in current_url and "auth" not in current_url.lower():
                login_form = await page.query_selector('.auth-form, [class*="auth-page"]')
                if not login_form:
                    logger.info("Authorization successful (URL check)")
                    return (True, False)

            # Проверяем 2FA форму
            password_input = await page.query_selector(
                'input[type="password"], [class*="password"], [placeholder*="Password"], [placeholder*="пароль"]'
            )
            if password_input:
                logger.info("2FA password required")
                return (False, True)

            # Проверяем ошибки
            error_element = await page.query_selector('[class*="error"], .error-message')
            if error_element:
                error_text = await error_element.inner_text()
                logger.error(f"Error detected: {error_text}")
                return (False, False)

            await asyncio.sleep(1)

            if i % 10 == 0 and i > 0:
                logger.debug(f"Still waiting for auth... ({i}s)")

        return (False, False)

    async def _handle_2fa(self, page, password: str) -> bool:
        """
        FIX-005: Улучшенный ввод 2FA пароля.

        Изменения:
        - Расширенный список селекторов (Telegram Web K/A)
        - Проверка visibility + enabled + bounding_box
        - Использование НАЙДЕННОГО селектора (не hardcoded)
        - Упрощённый ввод (delay=50ms)
        - Debug скриншоты с timestamp
        """
        logger.info("Entering 2FA password...")

        # FIX-005: Расширенный список селекторов для разных версий Telegram Web
        password_selectors = [
            'input[type="password"].input-field-input',  # Telegram Web K
            'input[name="notsearch_password"]',  # Telegram Web K by name
            'input[type="password"]:not(.stealthy)',  # Exclude hidden inputs
            '.input-field-input[type="password"]',
            'input[placeholder="Password"]',
            'input[placeholder*="assword"]',
            'input[autocomplete="current-password"]',
            # Telegram Web A / mobile
            "input.PasswordForm__input",
            'input[data-test="password-input"]',
            # Generic fallbacks
            'input[type="password"]',
        ]

        found_selector = None
        password_input = None

        # FIX-005: Увеличено время ожидания с 10 до 15 секунд
        for attempt in range(15):
            for selector in password_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        # FIX-005: Проверяем visibility + enabled + bounding_box
                        is_visible = await element.is_visible()
                        is_enabled = await element.is_enabled()
                        box = await element.bounding_box()

                        if is_visible and is_enabled and box and box["width"] > 0:
                            password_input = element
                            found_selector = selector
                            logger.debug(f"Found password input: {selector}")
                            break
                except Exception:
                    continue

            if password_input:
                break
            await asyncio.sleep(1)
            if attempt % 5 == 0 and attempt > 0:
                logger.debug(f"Looking for password field... ({attempt}s)")

        if not password_input or not found_selector:
            # Debug screenshot с timestamp
            timestamp = datetime.now().strftime("%H%M%S")
            debug_path = PROFILES_DIR / f"debug_2fa_notfound_{timestamp}.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(debug_path))
            logger.error(f"Password input not found! Screenshot: {debug_path}")
            return False

        try:
            # FIX-005: Используем НАЙДЕННЫЙ селектор, не hardcoded
            await page.click(found_selector, timeout=5000)
            await asyncio.sleep(0.3)
            logger.debug("Clicked password input")

            # FIX-005: Упрощённый ввод - быстрый но с небольшой задержкой
            # 50ms delay достаточен для большинства сайтов
            await page.keyboard.type(password, delay=50)
            logger.debug("Password typed")

            # Короткая пауза перед Enter
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            logger.debug("Pressed Enter to submit")

        except Exception as e:
            error_msg = str(e).encode("ascii", "replace").decode("ascii")
            logger.exception(f"Password input failed: {error_msg}")
            # Debug screenshot
            timestamp = datetime.now().strftime("%H%M%S")
            debug_path = PROFILES_DIR / f"debug_2fa_error_{timestamp}.png"
            await page.screenshot(path=str(debug_path))
            return False

        logger.info("Password submitted, waiting for response...")

        # Ждём результата - увеличено время для медленных соединений
        # Также ждём пока кнопка перестанет показывать "PLEASE WAIT..."
        for _wait_attempt in range(15):  # До 15 секунд
            await asyncio.sleep(1)

            # Проверяем не исчезла ли форма пароля (успешный вход)
            password_still_visible = await page.query_selector('input[type="password"]')
            if not password_still_visible:
                logger.info("Password form disappeared - likely successful")
                break

            # Check for INCORRECT PASSWORD on submit button
            incorrect_btn = await page.query_selector(
                'button:has-text("INCORRECT PASSWORD"), button:has-text("INCORRECT")'
            )
            if incorrect_btn:
                logger.error("2FA error: INCORRECT PASSWORD")
                return False

            # Проверяем loading state
            loading_btn = await page.query_selector('button:has-text("PLEASE WAIT"), button:has-text("Loading")')
            if not loading_btn:
                # Кнопка не в loading state - можем проверить результат
                break

        # Debug screenshot
        timestamp = datetime.now().strftime("%H%M%S")
        debug_path = PROFILES_DIR / f"debug_2fa_after_{timestamp}.png"
        await page.screenshot(path=str(debug_path))
        logger.debug(f"Debug screenshot: {debug_path}")

        # Проверяем ошибки
        error_selectors = [
            '[class*="error"]',
            ".error",
            ".input-field-error",
            '[class*="shake"]',
        ]
        for selector in error_selectors:
            try:
                error = await page.query_selector(selector)
                if error:
                    is_visible = await error.is_visible()
                    if is_visible:
                        error_text = await error.inner_text()
                        if error_text.strip():
                            logger.error(f"2FA error: {error_text}")
                            return False
            except Exception:
                pass

        return True

    async def authorize(self, password_2fa: str | None = None, headless: bool = False) -> AuthResult:
        """
        Выполняет полный цикл QR-авторизации.

        Args:
            password_2fa: Пароль 2FA если известен
            headless: Headless режим браузера

        Returns:
            AuthResult с результатом
        """
        # FIX #3: DEVICE SYNC - передаём device config в browser manager
        profile = self.browser_manager.get_profile(self.account.name, self.account.proxy)

        logger.info("=" * 60)
        logger.info("TELEGRAM WEB AUTHORIZATION")
        logger.info(f"Account: {self.account.name}")
        logger.info(f"Profile: {profile.name}")
        logger.debug(f"Device: {self.account.device.device_model} / {self.account.device.system_version}")
        logger.info("=" * 60)

        # Pre-check: if profile already has valid auth in storage_state.json,
        # skip browser launch entirely. This prevents re-migration timeouts
        # when retry-failed picks up accounts that were partially completed
        # (storage_state saved but authorize() killed by watchdog).
        if self._is_profile_already_authorized(profile):
            logger.info("Profile already has web auth — verifying session and skipping browser")
            client = None
            try:
                client = await self._create_telethon_client()
                self._client = client
                await self._set_authorization_ttl(client)
                telethon_alive = await self._verify_telethon_session(client)
            except Exception as e:
                logger.warning("Telethon check failed for pre-authorized profile: %s", e)
                telethon_alive = False
            finally:
                if client:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5)
                    except BaseException:
                        pass
            return AuthResult(
                success=True,
                profile_name=profile.name,
                required_2fa=False,
                telethon_alive=telethon_alive,
            )

        client = None
        browser_ctx = None
        watchdog = None

        try:
            # 1. Подключаем Telethon client
            self._status("[1/6] Connecting Telethon...")
            client = await self._create_telethon_client()
            self._client = client

            # 2. Запускаем браузер с синхронизированной ОС
            self._status("[2/6] Launching browser...")
            # FIX #3: Передаём os_list для консистентности
            browser_extra_args = {
                "os": self.account.device.browser_os_list,
            }
            browser_ctx = await self.browser_manager.launch(profile, headless=headless, extra_args=browser_extra_args)
            page = await browser_ctx.new_page()

            # Start thread-based watchdog to kill browser if authorize() hangs.
            # Needed because Playwright pipe I/O can block asyncio event loop
            # on Windows, preventing asyncio.timeout() from firing.
            if browser_ctx._browser_pid or browser_ctx._driver_pid:
                watchdog = BrowserWatchdog(
                    driver_pid=browser_ctx._driver_pid,
                    browser_pid=browser_ctx._browser_pid,
                    profile_name=profile.name,
                )
                watchdog.start()
            else:
                logger.warning(
                    "No browser PIDs available — watchdog disabled for '%s'",
                    profile.name,
                )

            # Устанавливаем viewport для корректного отображения QR
            await page.set_viewport_size({"width": 1280, "height": 800})

            # 3. Открываем Telegram Web
            self._status("[3/6] Opening Telegram Web...")
            try:
                await page.goto(self.TELEGRAM_WEB_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                error_str = str(e).lower()
                # Fail fast for non-recoverable network/proxy errors
                fatal_patterns = (
                    "err_proxy",
                    "err_tunnel",
                    "ns_error_proxy",
                    "err_name_not_resolved",
                    "connection_refused",
                    "target closed",
                    "net::err_name",
                    "net::err_connection",
                )
                if any(p in error_str for p in fatal_patterns):
                    raise RuntimeError(f"Page load failed (non-recoverable): {e}") from e
                logger.warning("Page load warning (will retry): %s", e)

            # Ждём загрузки страницы (до 15 секунд)
            logger.debug("Waiting for page to load...")
            for i in range(15):
                await asyncio.sleep(1)
                # Проверяем что страница загрузилась
                body = await page.query_selector("body")
                if body:
                    html = await page.content()
                    if "telegram" in html.lower() or "qr" in html.lower() or "password" in html.lower():
                        logger.debug(f"Page loaded after {i + 1}s")
                        break

            await asyncio.sleep(3)  # Дополнительная пауза для рендеринга

            # Проверяем текущее состояние страницы с повторными попытками
            # Возможно профиль уже авторизован или требует 2FA
            current_state = "unknown"
            for state_check in range(10):
                current_state = await self._check_page_state(page)
                if current_state == "dead":
                    break
                if current_state != "unknown" and current_state != "loading":
                    break
                await asyncio.sleep(1)
                if state_check % 3 == 0 and state_check > 0:
                    logger.debug(f"Checking page state... ({state_check}s)")

            logger.info(f"Current page state: {current_state}")

            if current_state == "dead":
                logger.error("Browser crashed or disconnected during auth")
                return AuthResult(success=False, profile_name=profile.name, error="Browser crashed (target closed)")

            if current_state == "authorized":
                logger.info("Already authorized! Skipping QR login.")
                await self._set_authorization_ttl(client)
                if browser_ctx:
                    browser_ctx.save_state_on_close = True
                return AuthResult(
                    success=True,
                    profile_name=profile.name,
                    required_2fa=False,
                    telethon_alive=await self._verify_telethon_session(client),
                )

            if current_state == "2fa_required":
                logger.info("2FA required (session from previous run)")
                if password_2fa:
                    fa_success = await self._handle_2fa(page, password_2fa)
                    if fa_success:
                        success, _ = await self._wait_for_auth_complete(page, timeout=30)
                        if success:
                            await self._set_authorization_ttl(client)
                            if browser_ctx:
                                browser_ctx.save_state_on_close = True
                            return AuthResult(
                                success=True,
                                profile_name=profile.name,
                                required_2fa=True,
                                telethon_alive=await self._verify_telethon_session(client),
                            )
                        # 2FA entered but auth didn't complete
                        return AuthResult(
                            success=False,
                            profile_name=profile.name,
                            required_2fa=True,
                            error="2FA accepted but authorization did not complete",
                        )
                    else:
                        # Wrong password or 2FA form error
                        return AuthResult(
                            success=False,
                            profile_name=profile.name,
                            required_2fa=True,
                            error="2FA password incorrect or rejected",
                        )
                else:
                    logger.info("2FA password not provided, waiting for manual input...")
                    success, _ = await self._wait_for_auth_complete(page)
                    if success:
                        await self._set_authorization_ttl(client)
                        if browser_ctx:
                            browser_ctx.save_state_on_close = True
                    return AuthResult(
                        success=success,
                        profile_name=profile.name,
                        required_2fa=True,
                        error=None if success else "2FA password required",
                        telethon_alive=await self._verify_telethon_session(client),
                    )

            # FIX-2.3: Recovery reload for "unknown"/"loading" state
            if current_state in ("unknown", "loading"):
                logger.warning(
                    f"Page state is '{current_state}' after 10s checks — "
                    "attempting fresh navigation before QR extraction."
                )
                try:
                    await page.goto(
                        self.TELEGRAM_WEB_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(3)
                    current_state = await self._check_page_state(page)
                    logger.info("Page state after recovery reload: %s", current_state)
                    if current_state == "dead":
                        return AuthResult(
                            success=False, profile_name=profile.name, error="Browser crashed during recovery reload"
                        )
                    if current_state == "authorized":
                        logger.info("Already authorized after recovery reload!")
                        await self._set_authorization_ttl(client)
                        if browser_ctx:
                            browser_ctx.save_state_on_close = True
                        return AuthResult(
                            success=True,
                            profile_name=profile.name,
                            required_2fa=False,
                            telethon_alive=await self._verify_telethon_session(client),
                        )
                    # FIX: Check 2FA state after recovery reload
                    if current_state == "2fa_required":
                        logger.info("2FA required after recovery reload")
                        if password_2fa:
                            success = await self._handle_2fa(page, password_2fa)
                            if success:
                                auth_ok, _ = await self._wait_for_auth_complete(page, timeout=30)
                                if auth_ok:
                                    await self._set_authorization_ttl(client)
                                    if browser_ctx:
                                        browser_ctx.save_state_on_close = True
                                    return AuthResult(
                                        success=True,
                                        profile_name=profile.name,
                                        required_2fa=True,
                                        telethon_alive=await self._verify_telethon_session(client),
                                    )
                        return AuthResult(
                            success=False, profile_name=profile.name, required_2fa=True, error="2FA password required"
                        )
                except Exception as e:
                    error_str = str(e).lower()
                    # Unified with primary goto fatal_patterns (line 1689)
                    fatal_patterns = (
                        "err_proxy",
                        "err_tunnel",
                        "ns_error_proxy",
                        "err_name_not_resolved",
                        "connection_refused",
                        "target closed",
                        "net::err_name",
                        "net::err_connection",
                    )
                    if any(p in error_str for p in fatal_patterns):
                        return AuthResult(
                            success=False,
                            profile_name=profile.name,
                            error=f"Recovery reload failed (non-recoverable): {sanitize_error(str(e))}",
                        )
                    logger.warning("Recovery reload failed (will proceed to QR): %s", e)

            # 4. Ждём и декодируем QR (FIX #4: с retry)
            self._status("[4/6] Extracting QR token...")
            token = await self._extract_qr_token_with_retry(page)

            if not token:
                # FIX-C4: Re-check if browser became authorized during QR extraction
                # (e.g., delayed AcceptLoginToken took effect, or profile was already logged in)
                final_state = await self._check_page_state(page)
                if final_state == "authorized":
                    logger.info("Browser authorized during QR extraction cycle")
                    await self._set_authorization_ttl(client)
                    if browser_ctx:
                        browser_ctx.save_state_on_close = True
                    return AuthResult(
                        success=True,
                        profile_name=profile.name,
                        telethon_alive=await self._verify_telethon_session(client),
                    )
                if final_state == "2fa_required":
                    return AuthResult(
                        success=False,
                        profile_name=profile.name,
                        required_2fa=True,
                        error="2FA required but no password provided",
                    )
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error=f"Failed to extract QR token after {self.QR_MAX_RETRIES} attempts",
                )

            # 5. Подтверждаем токен через Telethon
            self._status("[5/6] Accepting login token...")
            accepted, accept_error = await self._accept_token(client, token)

            if not accepted:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error=accept_error or "Failed to accept login token",
                )

            # Обновляем страницу чтобы браузер увидел авторизацию
            await asyncio.sleep(2)  # Даём время на синхронизацию
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"Page reload after accept: {e}")

            # Ждём авторизацию в браузере
            success, need_2fa = await self._wait_for_auth_complete(page)

            # Обрабатываем 2FA
            if need_2fa:
                if password_2fa:
                    logger.info("Handling 2FA...")
                    fa_success = await self._handle_2fa(page, password_2fa)

                    if fa_success:
                        # Ждём окончательную авторизацию
                        success, _ = await self._wait_for_auth_complete(page, timeout=30)
                else:
                    logger.info("2FA required but password not provided.")
                    logger.info("Please enter password manually in browser...")

                    # Ждём ручной ввод
                    success, _ = await self._wait_for_auth_complete(page)

            # FIX #2: Проверяем что Telethon сессия жива после авторизации браузера
            self._status("[6/6] Verifying session...")
            telethon_alive = await self._verify_telethon_session(client)

            if success:
                await self._set_authorization_ttl(client)

                # Получаем инфо о пользователе из браузера
                user_info = await self._get_browser_user_info(page)

                # FIX #6: Mark browser context for storage_state save on close
                if browser_ctx:
                    browser_ctx.save_state_on_close = True

                return AuthResult(
                    success=True,
                    profile_name=profile.name,
                    required_2fa=need_2fa,
                    user_info=user_info,
                    telethon_alive=telethon_alive,
                )
            else:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error="Authorization did not complete",
                    required_2fa=need_2fa,
                    telethon_alive=telethon_alive,
                )

        except BaseException as e:
            # BaseException catches CancelledError (Python 3.11+) for proper cleanup
            if isinstance(e, asyncio.CancelledError):
                logger.warning("authorize() cancelled for '%s'", profile.name)
            else:
                logger.error("Authorization failed for '%s': %s", profile.name, sanitize_error(str(e)))
            result = AuthResult(success=False, profile_name=profile.name, error=sanitize_error(str(e)))
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                raise  # re-raise after finally cleanup
            return result

        finally:
            # Cancel watchdog first (before cleanup attempts that might also hang)
            if watchdog:
                watchdog.cancel()

            # FIX #5: ERROR RECOVERY - гарантированный cleanup
            if client:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                    logger.debug("Telethon client disconnected")
                except BaseException as e:
                    logger.warning(f"Error disconnecting client: {e}")

            if browser_ctx:
                try:
                    await browser_ctx.close()
                except BaseException as e:
                    logger.warning(f"Error closing browser: {e}")

    async def _get_browser_user_info(self, page) -> dict | None:
        """Извлекает информацию о пользователе из браузера"""
        try:
            # Пробуем получить имя из UI
            name_element = await page.query_selector('.user-title, [class*="peer-title"], .profile-name')
            if name_element:
                name = await name_element.inner_text()
                return {"name": name.strip()}
        except Exception:
            # UI element not found, not accessible, or browser dead
            pass
        return None


# FIX #6: MULTI-ACCOUNT COOLDOWN
# Safety: 60-120s random cooldown between accounts (research: 10-20 logins/hour)
DEFAULT_ACCOUNT_COOLDOWN = 90  # базовое значение в секундах (центр диапазона)
MIN_COOLDOWN = 60  # минимальный cooldown
MAX_COOLDOWN = 180  # максимальный cooldown


def get_randomized_cooldown(base_cooldown: float = DEFAULT_ACCOUNT_COOLDOWN) -> float:
    """
    Генерирует randomized cooldown для избежания детекции паттернов.

    Использует log-normal distribution с центром около base_cooldown.
    Результат clamped to [base_cooldown * 0.5, base_cooldown * 2].
    For production (base >= MIN_COOLDOWN), also respects MIN/MAX_COOLDOWN.

    Args:
        base_cooldown: Базовое значение cooldown в секундах

    Returns:
        Randomized cooldown в секундах
    """
    # Log-normal distribution: большинство значений около base_cooldown,
    # но с длинным хвостом для редких длинных пауз
    mu = math.log(base_cooldown) - 0.5 * 0.3**2  # Корректировка для среднего
    sigma = 0.3  # Стандартное отклонение в log-space

    delay = random.lognormvariate(mu, sigma)

    # Clamp to reasonable range around base_cooldown
    # For testing (--cooldown 10): range [5, 20]
    # For production (--cooldown 90): range [60, 180]
    low = max(base_cooldown * 0.5, MIN_COOLDOWN) if base_cooldown >= MIN_COOLDOWN else base_cooldown * 0.5
    high = min(base_cooldown * 2, MAX_COOLDOWN)
    return max(low, min(delay, high))


async def migrate_account(
    account_dir: Path,
    password_2fa: str | None = None,
    headless: bool = False,
    proxy_override: str | None = None,
    browser_manager: BrowserManager | None = None,
    on_status: Callable[[str], None] | None = None,
) -> AuthResult:
    """
    Мигрирует один аккаунт из session в browser profile.

    Args:
        account_dir: Директория с session, api.json, ___config.json
        password_2fa: Пароль 2FA
        headless: Headless режим
        proxy_override: Прокси строка из БД (перезаписывает ___config.json)
        browser_manager: Shared BrowserManager instance (creates new if None).
        on_status: Optional callback for progress updates (e.g. GUI log).

    Returns:
        AuthResult
    """
    account = AccountConfig.load(account_dir)
    if proxy_override == "NONE":
        # --no-proxy mode: strip all proxies (DB + ___config.json)
        account.proxy = None
    elif proxy_override is not None:
        account.proxy = proxy_override
    auth = TelegramAuth(account, browser_manager=browser_manager, on_status=on_status)
    return await auth.authorize(password_2fa=password_2fa, headless=headless)


async def migrate_accounts_batch(
    account_dirs: list[Path],
    password_2fa: str | None = None,
    headless: bool = False,
    cooldown: int = DEFAULT_ACCOUNT_COOLDOWN,
    on_result: Callable[["AuthResult"], Any] | None = None,
    passwords_map: dict[str, str] | None = None,
    proxy_map: dict[str, str] | None = None,
) -> list[AuthResult]:
    """
    FIX #6: Мигрирует несколько аккаунтов с cooldown между ними.

    Args:
        account_dirs: Список директорий аккаунтов
        password_2fa: Общий 2FA пароль (если одинаковый)
        headless: Headless режим
        cooldown: Секунды между аккаунтами (default 45)
        on_result: FIX-4.2: Callback called after each account (for crash-safe DB updates)
        passwords_map: FIX-H9: Per-account 2FA passwords {account_name: password}
        proxy_map: DB proxy overrides {account_name: "socks5:host:port:user:pass"}

    Returns:
        Список AuthResult
    """
    results = []
    # FIX D2: Shared BrowserManager for all accounts in batch
    shared_browser_manager = BrowserManager()

    try:
        for i, account_dir in enumerate(account_dirs):
            logger.info("=" * 60)
            logger.info(f"ACCOUNT {i + 1}/{len(account_dirs)}: {account_dir.name}")
            logger.info("=" * 60)

            # FIX-H9: Per-account password from passwords_map overrides global password_2fa
            account_password = password_2fa
            if passwords_map and account_dir.name in passwords_map:
                account_password = passwords_map[account_dir.name]

            # DB proxy override (takes priority over ___config.json)
            account_proxy = proxy_map.get(account_dir.name) if proxy_map else None

            result = await migrate_account(
                account_dir=account_dir,
                password_2fa=account_password,
                headless=headless,
                proxy_override=account_proxy,
                browser_manager=shared_browser_manager,
            )
            results.append(result)

            # FIX-4.2: Call per-account callback for crash-safe DB updates
            if on_result:
                try:
                    cb_result = on_result(result)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                except Exception as e:
                    logger.warning(f"on_result callback error: {e}")

            # FIX #6: Randomized cooldown между аккаунтами (кроме последнего)
            if i < len(account_dirs) - 1:
                jittered_cooldown = get_randomized_cooldown(cooldown)
                logger.info(f"Cooldown {jittered_cooldown:.1f}s before next account (base: {cooldown}s)...")
                await asyncio.sleep(jittered_cooldown)
    finally:
        await shared_browser_manager.close_all()

    return results


# Progress callback type for parallel migration
ProgressCallback = Callable[[int, int, Optional["AuthResult"]], None]


class CircuitBreaker:
    """
    Circuit breaker for cascade failure protection.

    When too many consecutive failures occur, the circuit "opens"
    and pauses new operations to prevent overwhelming the system.

    Usage:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60)

        if not breaker.can_proceed():
            await asyncio.sleep(breaker.time_until_reset())
            continue

        result = await do_operation()
        if not result.success:
            breaker.record_failure()
        else:
            breaker.record_success()
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        """
        Args:
            failure_threshold: Number of consecutive failures before opening
            reset_timeout: Seconds to wait before trying again after opening
        """
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._is_open = False
        # FIX #4: Only one worker probes during HALF-OPEN state
        self._half_open_probing = False
        self._probe_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Check if circuit is currently open (blocking operations)"""
        return self._is_open

    @property
    def consecutive_failures(self) -> int:
        """Current count of consecutive failures"""
        return self._consecutive_failures

    def record_failure(self) -> None:
        """Record a failure. Opens circuit if threshold exceeded."""
        import time

        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()

        if self._consecutive_failures >= self._failure_threshold:
            if not self._is_open:
                logger.warning(
                    f"Circuit breaker OPEN after {self._consecutive_failures} "
                    f"consecutive failures. Pausing for {self._reset_timeout}s"
                )
            self._is_open = True

    def record_success(self) -> None:
        """Record a success. Resets failure counter and closes circuit."""
        if self._consecutive_failures > 0 or self._is_open:
            logger.info("Circuit breaker: success recorded, resetting state")
        self._consecutive_failures = 0
        self._is_open = False
        # FIX #4: Probe completed successfully, release flag
        self._half_open_probing = False

    def can_proceed(self) -> bool:
        """
        Check if operations can proceed.

        Returns True if:
        - Circuit is closed (normal operation)
        - Circuit is open but reset_timeout has elapsed (half-open)

        FIX #4: In half-open state, only one caller proceeds (via lock).
        Others get False and must wait.
        """
        if not self._is_open:
            return True

        import time

        elapsed = time.monotonic() - self._last_failure_time

        if elapsed >= self._reset_timeout:
            # FIX #4: Only allow one probe during half-open.
            if self._half_open_probing:
                # Another worker is already probing
                return False
            logger.info(
                f"Circuit breaker: reset timeout elapsed ({elapsed:.1f}s >= {self._reset_timeout}s), allowing probe"
            )
            return True

        return False

    async def acquire_half_open_probe(self) -> bool:
        """
        FIX #4: Acquire the half-open probe flag (non-blocking).

        Call this from the worker BEFORE doing the actual migration
        when can_proceed() returned True and circuit is open.
        Returns True if this worker is the probe, False if another worker beat us.
        Uses asyncio.Lock to prevent race conditions across await points.
        """
        async with self._probe_lock:
            if not self._is_open:
                return True  # Circuit closed, no probe needed
            if self._half_open_probing:
                return False
            self._half_open_probing = True
            return True

    def release_half_open_probe(self) -> None:
        """FIX #4: Release the half-open probe flag after probe completes."""
        self._half_open_probing = False

    def time_until_reset(self) -> float:
        """Seconds until circuit breaker resets (0 if closed)"""
        if not self._is_open:
            return 0.0

        import time

        elapsed = time.monotonic() - self._last_failure_time
        remaining = self._reset_timeout - elapsed
        return max(0.0, remaining)

    def reset(self) -> None:
        """Manually reset the circuit breaker"""
        self._consecutive_failures = 0
        self._is_open = False
        self._last_failure_time = 0.0
        # FIX #4: Reset probe flag
        self._half_open_probing = False


class ParallelMigrationController:
    """
    Controller for parallel migration with graceful shutdown support.

    Usage:
        controller = ParallelMigrationController(max_concurrent=10)

        # In signal handler:
        signal.signal(signal.SIGINT, lambda s, f: controller.request_shutdown())

        results = await controller.run(account_dirs)
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        cooldown: float = 5.0,
        resource_monitor: Optional["ResourceMonitor"] = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self.max_concurrent = max_concurrent
        # Enforce minimum cooldown to prevent mass bans at scale
        self.cooldown = max(cooldown, MIN_COOLDOWN)
        self.resource_monitor = resource_monitor
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._shutdown_requested = False
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._completed = 0
        self._total = 0
        self._paused_for_resources = False
        self._paused_for_circuit_breaker = False

    @property
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @property
    def progress(self) -> tuple[int, int]:
        """Returns (completed, total)"""
        return (self._completed, self._total)

    def request_shutdown(self):
        """Request graceful shutdown - finish running, don't start new"""
        logger.info("Shutdown requested - finishing active tasks...")
        self._shutdown_requested = True

    async def run(
        self,
        account_dirs: list[Path],
        password_2fa: str | None = None,
        headless: bool = False,
        on_progress: ProgressCallback | None = None,
        passwords_map: dict[str, str] | None = None,
        proxy_map: dict[str, str] | None = None,
    ) -> list[AuthResult]:
        """
        Run parallel migration with shutdown support.

        FIX #13: Uses shared BrowserManager with cleanup in finally.
        FIX #16: Cooldown after migration completes (inside semaphore),
                 not between create_task() calls.

        Returns results for completed accounts (may be partial on shutdown).
        """
        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: dict[int, AuthResult] = {}
        lock = asyncio.Lock()
        self._total = len(account_dirs)
        self._completed = 0
        self._shutdown_requested = False

        # FIX #13: Shared BrowserManager for all workers
        browser_manager = BrowserManager()

        async def migrate_one(index: int, account_dir: Path):
            # Check shutdown before acquiring semaphore
            if self._shutdown_requested:
                return None

            # Wait for circuit breaker if open (cascade failure protection)
            if not self.circuit_breaker.can_proceed():
                wait_time = self.circuit_breaker.time_until_reset()
                if wait_time > 0:
                    if not self._paused_for_circuit_breaker:
                        self._paused_for_circuit_breaker = True
                        logger.warning(
                            f"Circuit breaker open, waiting {wait_time:.1f}s "
                            f"after {self.circuit_breaker.consecutive_failures} failures"
                        )
                    await asyncio.sleep(wait_time)
                    self._paused_for_circuit_breaker = False

            # Wait for resources if monitor provided
            if self.resource_monitor:
                wait_count = 0
                while not self.resource_monitor.can_launch_more():
                    if self._shutdown_requested:
                        return None
                    if wait_count == 0:
                        self._paused_for_resources = True
                        logger.info(f"Waiting for resources: {self.resource_monitor.format_status()}")
                    await asyncio.sleep(5)
                    wait_count += 1
                    if wait_count > 60:  # 5 min timeout
                        async with lock:
                            results[index] = AuthResult(
                                success=False, profile_name=account_dir.name, error="Timeout waiting for resources"
                            )
                            self._completed += 1
                        return results[index]
                self._paused_for_resources = False

            # Per-task timeout to prevent stuck tasks from blocking semaphore forever
            TASK_TIMEOUT = 300  # 5 minutes per account

            async with semaphore:
                # Check again after acquiring
                if self._shutdown_requested:
                    return None

                try:
                    async with asyncio.timeout(TASK_TIMEOUT):
                        # FIX #13: Pass shared browser_manager
                        # FIX-H9: Per-account password from passwords_map overrides global
                        account_password = password_2fa
                        if passwords_map and account_dir.name in passwords_map:
                            account_password = passwords_map[account_dir.name]
                        # DB proxy override (takes priority over ___config.json)
                        account_proxy = proxy_map.get(account_dir.name) if proxy_map else None
                        result = await migrate_account(
                            account_dir=account_dir,
                            password_2fa=account_password,
                            headless=headless,
                            browser_manager=browser_manager,
                            proxy_override=account_proxy,
                        )
                except TimeoutError:
                    logger.warning(f"Task timeout after {TASK_TIMEOUT}s for {account_dir.name}")
                    result = AuthResult(
                        success=False, profile_name=account_dir.name, error=f"Task timeout after {TASK_TIMEOUT}s"
                    )
                except Exception as e:
                    result = AuthResult(success=False, profile_name=account_dir.name, error=sanitize_error(str(e)))

                # Record result for circuit breaker
                if result.success:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure()

                async with lock:
                    results[index] = result
                    self._completed += 1
                    if on_progress:
                        try:
                            cb_result = on_progress(self._completed, self._total, result)
                            if cb_result is not None and (
                                asyncio.iscoroutine(cb_result) or asyncio.isfuture(cb_result)
                            ):
                                await cb_result
                        except Exception as e:
                            logger.warning(f"Progress callback error: {e}")

                # FIX #16: Cooldown AFTER migration completes (inside semaphore),
                # ensuring actual gaps between account operations, not just
                # between task creation.
                if not self._shutdown_requested and self.cooldown > 0:
                    jittered = get_randomized_cooldown(self.cooldown)
                    await asyncio.sleep(jittered)

                return result

        try:
            # Create and track tasks (no cooldown between create_task calls)
            tasks = []
            for i, account_dir in enumerate(account_dirs):
                if self._shutdown_requested:
                    break

                task = asyncio.create_task(migrate_one(i, account_dir))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                tasks.append(task)

            # Dynamic timeout: at least 2h, scales with batch size
            # Formula: (accounts / max_concurrent) * cooldown_per_account * safety_factor
            estimated_time = (len(account_dirs) / self.max_concurrent) * self.cooldown * 2
            BATCH_TIMEOUT = max(7200, int(estimated_time))  # minimum 2 hours
            logger.info(f"Batch timeout: {BATCH_TIMEOUT}s for {len(account_dirs)} accounts")

            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=BATCH_TIMEOUT, return_when=asyncio.ALL_COMPLETED)

                # Cancel any stuck tasks that exceeded batch timeout
                if pending:
                    logger.warning(f"Batch timeout: cancelling {len(pending)} stuck tasks")
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        finally:
            # FIX #13: Always clean up shared BrowserManager
            try:
                await browser_manager.close_all()
            except BaseException as e:
                logger.warning("BrowserManager cleanup error in ParallelMigrationController: %s", e)

        # Return results in order (only completed ones)
        ordered_results = []
        for i in range(len(account_dirs)):
            if i in results:
                ordered_results.append(results[i])
            elif self._shutdown_requested:
                # Mark as skipped due to shutdown
                ordered_results.append(
                    AuthResult(success=False, profile_name=account_dirs[i].name, error="Skipped due to shutdown")
                )

        return ordered_results


async def migrate_accounts_parallel(
    account_dirs: list[Path],
    password_2fa: str | None = None,
    headless: bool = False,
    max_concurrent: int = 10,
    cooldown: float = 5.0,
    on_progress: ProgressCallback | None = None,
) -> list[AuthResult]:
    """
    Migrates multiple accounts in parallel with concurrency control.

    Args:
        account_dirs: List of account directories
        password_2fa: Shared 2FA password (if same for all)
        headless: Run browsers in headless mode
        max_concurrent: Maximum parallel browser instances (default 10)
        cooldown: Seconds between starting new tasks (rate limiting)
        on_progress: Callback(completed, total, result) for progress updates

    Returns:
        List of AuthResult in same order as account_dirs
    """
    # Deduplicate to prevent AUTH_KEY_DUPLICATED from parallel same-session access
    account_dirs = list(dict.fromkeys(account_dirs))

    semaphore = asyncio.Semaphore(max_concurrent)
    results: dict[int, AuthResult] = {}
    completed = 0
    total = len(account_dirs)
    lock = asyncio.Lock()
    # Shared BrowserManager for LRU eviction across all parallel workers
    shared_browser_manager = BrowserManager()

    async def migrate_with_semaphore(index: int, account_dir: Path):
        nonlocal completed
        async with semaphore:
            try:
                result = await migrate_account(
                    account_dir=account_dir,
                    password_2fa=password_2fa,
                    headless=headless,
                    browser_manager=shared_browser_manager,
                )
            except BaseException as e:
                # BaseException catches CancelledError (Python 3.11+)
                result = AuthResult(success=False, profile_name=account_dir.name, error=sanitize_error(str(e)))
                # Re-raise CancelledError after recording result
                if isinstance(e, asyncio.CancelledError):
                    async with lock:
                        results[index] = result
                        completed += 1
                    raise

            async with lock:
                results[index] = result
                completed += 1
                if on_progress:
                    try:
                        on_progress(completed, total, result)
                    except Exception as e:
                        logger.warning(f"Progress callback error: {e}")

            return result

    try:
        # Create tasks with staggered start (rate limiting with jitter)
        tasks = []
        for i, account_dir in enumerate(account_dirs):
            task = asyncio.create_task(migrate_with_semaphore(i, account_dir))
            tasks.append(task)
            # Stagger task creation with randomized delay to avoid pattern detection
            if i < len(account_dirs) - 1 and cooldown > 0:
                jittered = get_randomized_cooldown(cooldown)
                await asyncio.sleep(jittered)

        # Wait for all to complete
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Always clean up shared BrowserManager
        try:
            await shared_browser_manager.close_all()
        except BaseException as e:
            logger.warning("BrowserManager cleanup error in migrate_accounts_parallel: %s", e)

    # Return results in original order
    return [
        results.get(i, AuthResult(success=False, profile_name=str(account_dirs[i].name), error="Task cancelled"))
        for i in range(len(account_dirs))
    ]


async def main():
    """CLI для тестирования"""
    import argparse

    parser = argparse.ArgumentParser(description="Telegram Web QR Authorization")
    parser.add_argument("--account", required=True, help="Path to account directory")
    parser.add_argument("--password", help="2FA password if needed")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")

    args = parser.parse_args()

    account_path = Path(args.account)
    if not account_path.exists():
        logger.error("Account directory not found: %s", account_path)
        sys.exit(1)

    result = await migrate_account(account_dir=account_path, password_2fa=args.password, headless=args.headless)

    logger.info("=" * 60)
    logger.info("RESULT")
    logger.info("=" * 60)
    logger.info("Success: %s", result.success)
    logger.info("Profile: %s", result.profile_name)
    logger.info("Telethon alive: %s", result.telethon_alive)
    if result.error:
        logger.info("Error: %s", result.error)
    if result.required_2fa:
        logger.info("Required 2FA: Yes")
    if result.user_info:
        logger.info("User: %s", result.user_info)

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    asyncio.run(main())
