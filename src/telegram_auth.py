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
import json
import io
import logging
import random
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Callable, Set, TYPE_CHECKING

# Logger for this module
logger = logging.getLogger(__name__)

# Suppress noisy Telethon internal logs
logging.getLogger('telethon').setLevel(logging.ERROR)

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
    from telethon.sessions import SQLiteSession
    from telethon.tl.functions.auth import AcceptLoginTokenRequest
    from telethon.tl.functions.account import SetAuthorizationTTLRequest
    from telethon.errors import SessionPasswordNeededError, FloodWaitError
except ImportError as e:
    raise ImportError("telethon not installed. Run: pip install telethon") from e

from .browser_manager import BrowserManager, BrowserProfile, BrowserContext

if TYPE_CHECKING:
    from .resource_monitor import ResourceMonitor


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
        if 'windows' in sv:
            return 'windows'
        elif 'mac' in sv or 'darwin' in sv:
            return 'macos'
        elif 'linux' in sv or 'ubuntu' in sv:
            return 'linux'
        return 'windows'  # Default

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
    proxy: Optional[str] = None
    phone: Optional[str] = None
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
            with open(api_path, 'r', encoding='utf-8') as f:
                api_config = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in {api_path}: {e.msg}",
                e.doc, e.pos
            )

        # Проверяем обязательные поля
        if "api_id" not in api_config:
            raise KeyError(f"'api_id' not found in {api_path}")
        if "api_hash" not in api_config:
            raise KeyError(f"'api_hash' not found in {api_path}")

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
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    proxy = config.get("Proxy")
                    name = config.get("Name", name)
            except json.JSONDecodeError as e:
                # Config опциональный, но логируем ошибку
                logger.error(f"[AccountConfig] Invalid JSON in {config_path}: {e.msg} at line {e.lineno}")

        return cls(
            name=name,
            session_path=session_path,
            api_id=api_config["api_id"],
            api_hash=api_config["api_hash"],
            proxy=proxy,
            device=device,
        )


@dataclass
class AuthResult:
    """Результат авторизации"""
    success: bool
    profile_name: str
    error: Optional[str] = None
    required_2fa: bool = False
    user_info: Optional[Dict[str, Any]] = None
    telethon_alive: bool = False  # FIX #2: Session safety check


def decode_qr_from_screenshot(screenshot_bytes: bytes) -> Optional[bytes]:
    """
    Декодирует QR-код из скриншота.

    Args:
        screenshot_bytes: PNG скриншот в bytes

    Returns:
        Token bytes или None если QR не найден/не декодирован
    """
    from PIL import ImageOps, ImageEnhance, ImageFilter

    image = Image.open(io.BytesIO(screenshot_bytes))

    def extract_token(data):
        """Extract token bytes from tg://login URL"""
        if not data or 'tg://login?token=' not in data:
            return None
        token_b64 = data.split('token=')[1]
        if '&' in token_b64:
            token_b64 = token_b64.split('&')[0]
        padding = 4 - len(token_b64) % 4
        if padding != 4:
            token_b64 += '=' * padding
        return base64.urlsafe_b64decode(token_b64)

    # Try Node.js jsQR first (handles Telegram rounded corners better)
    try:
        import subprocess
        import tempfile
        import os

        temp_path = None
        try:
            # Save image to temp file
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                temp_path = f.name
                image.save(f, 'PNG')

            # Call Node.js decoder
            script_path = os.path.join(os.path.dirname(__file__), '..', 'decode_qr.js')
            result = subprocess.run(
                ['node', script_path, temp_path],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and result.stdout.strip():
                data = result.stdout.strip()
                if data != 'QR_NOT_FOUND' and 'tg://login?token=' in data:
                    token_bytes = extract_token(data)
                    if token_bytes:
                        logger.info("Decoded QR with jsQR (Node.js)")
                        return token_bytes
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as e:
        logger.debug("jsQR error: %s", e)

    # Try zxing-cpp with morphological preprocessing (handles Telegram dot-style QR)
    try:
        import zxingcpp

        def _zxing_decode_morph(pil_img):
            """Decode QR via zxing-cpp with scale + morph close for dot-style QR."""
            # 1. Try raw image
            results = zxingcpp.read_barcodes(pil_img)
            for r in results:
                if r.text and 'tg://login?token=' in r.text:
                    return r.text

            # 2. Scale up + binarize + morphological close
            # Two presets: high-contrast (dots) and low-contrast (thin lines)
            img_gray = np.array(pil_img.convert('L'))
            h, w = img_gray.shape
            scaled = cv2.resize(img_gray, (w * 4, h * 4), interpolation=cv2.INTER_NEAREST)

            for threshold in (128, 80):
                _, binary = cv2.threshold(scaled, threshold, 255, cv2.THRESH_BINARY)
                for ksize in (9, 13, 17):
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
                    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
                    results = zxingcpp.read_barcodes(Image.fromarray(closed))
                    for r in results:
                        if r.text and 'tg://login?token=' in r.text:
                            return r.text
            return None

        def _get_qr_crops(pil_img):
            """Get candidate QR crop regions for full-page screenshots."""
            w, h = pil_img.size
            crops = []

            # 1. Contour-based detection (multiple thresholds)
            gray_np = np.array(pil_img.convert('L'))
            for thr in (150, 120, 100):
                _, thresh = cv2.threshold(gray_np, thr, 255, cv2.THRESH_BINARY)
                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
                )
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

            # 2. Telegram Web K typical QR positions (center-right)
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
        img_np = np.array(image.convert('RGB'))
        cv_img = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        decoded, _, _ = aruco_detector.detectAndDecode(cv_img)
        if decoded and decoded[0]:
            for data in decoded:
                if data and 'tg://login?token=' in data:
                    token_bytes = extract_token(data)
                    if token_bytes:
                        logger.info("Decoded QR with QRCodeDetectorAruco")
                        return token_bytes
    except Exception as e:
        logger.debug(f"QRCodeDetectorAruco error: {e}")

    # Конвертируем PIL Image в numpy array для OpenCV
    def pil_to_cv2(pil_img):
        """Convert PIL Image to OpenCV format"""
        if pil_img.mode == '1':
            pil_img = pil_img.convert('L')
        if pil_img.mode == 'L':
            return np.array(pil_img)
        elif pil_img.mode == 'RGB':
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        elif pil_img.mode == 'RGBA':
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGBA2BGR)
        return np.array(pil_img.convert('RGB'))

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
                return obj.data.decode('utf-8')
        except Exception as e:
            logger.warning(f"[QR] pyzbar decode error: {type(e).__name__}: {e}")
        return None

    # Пробуем декодировать в разных вариантах с OpenCV/pyzbar
    # Telegram Web использует белый QR на тёмном фоне - нужно инвертировать
    variants = []

    # 1. Оригинал
    variants.append(("original", image))

    # 2. Grayscale
    gray = image.convert('L')
    variants.append(("grayscale", gray))

    # 3. Инвертированный RGB (для белого QR на тёмном)
    try:
        inverted_rgb = ImageOps.invert(image.convert('RGB'))
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
        threshold = gray.point(lambda x: 255 if x > 128 else 0, 'L')
        variants.append(("threshold", threshold))
        threshold_inv = gray.point(lambda x: 0 if x > 128 else 255, 'L')
        variants.append(("threshold_inv", threshold_inv))
    except Exception as e:
        logger.debug(f"[QR] threshold variant failed: {e}")

    for name, img_variant in variants:
        # Try OpenCV first (more reliable on Windows)
        try:
            cv_img = pil_to_cv2(img_variant)
            data = decode_with_opencv(cv_img)
            if data and 'tg://login?token=' in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    logger.info(f"Decoded QR with OpenCV using {name}")
                    return token_bytes
        except Exception as e:
            logger.debug(f"[QR] OpenCV decode failed for {name}: {e}")

        # Fallback to pyzbar if available
        try:
            data = decode_with_pyzbar(img_variant)
            if data and 'tg://login?token=' in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    logger.info(f"Decoded QR with pyzbar using {name}")
                    return token_bytes
        except Exception as e:
            logger.debug(f"[QR] pyzbar decode failed for {name}: {e}")

    return None


def extract_token_from_tg_url(url_str: str) -> Optional[bytes]:
    """
    Извлекает token bytes из tg://login URL.

    Args:
        url_str: URL в формате tg://login?token=BASE64TOKEN

    Returns:
        Token bytes или None
    """
    if not url_str or 'tg://login?token=' not in url_str:
        return None

    try:
        token_b64 = url_str.split('token=')[1]
        # Удаляем возможные лишние параметры
        if '&' in token_b64:
            token_b64 = token_b64.split('&')[0]
        # Добавляем padding
        padding = 4 - len(token_b64) % 4
        if padding != 4:
            token_b64 += '=' * padding
        return base64.urlsafe_b64decode(token_b64)
    except Exception as e:
        logger.debug(f"Error extracting token from URL: {e}")
        return None


def parse_telethon_proxy(proxy_str: str) -> Optional[tuple]:
    """
    Конвертирует прокси в формат Telethon.
    Input: socks5:host:port:user:pass
    Output: (socks.SOCKS5, host, port, True, user, pass)
    """
    if not proxy_str:
        return None

    import socks

    parts = proxy_str.split(":")
    if len(parts) == 5:
        proto, host, port, user, pwd = parts
        proxy_type = socks.SOCKS5 if 'socks5' in proto.lower() else socks.HTTP
        return (proxy_type, host, int(port), True, user, pwd)
    elif len(parts) == 3:
        proto, host, port = parts
        proxy_type = socks.SOCKS5 if 'socks5' in proto.lower() else socks.HTTP
        return (proxy_type, host, int(port))

    return None


class TelegramAuth:
    """
    Основной класс для QR-авторизации Telegram Web.
    """

    TELEGRAM_WEB_URL = "https://web.telegram.org/k/"
    QR_WAIT_TIMEOUT = 30  # секунд ждать появления QR
    AUTH_WAIT_TIMEOUT = 120  # секунд ждать завершения авторизации
    QR_MAX_RETRIES = 5  # FIX-008: Увеличено с 3 до 5 для надёжности
    QR_RETRY_DELAY = 5  # секунд между retry

    def __init__(self, account: AccountConfig, browser_manager: Optional[BrowserManager] = None):
        self.account = account
        self.browser_manager = browser_manager or BrowserManager()
        self._client: Optional[TelegramClient] = None

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
        session_path = self.account.session_path
        if session_path.exists():
            try:
                conn = sqlite3.connect(str(session_path), timeout=10)
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=10000')  # 10 секунд ожидания
                conn.close()
                logger.debug("SQLite WAL mode enabled for session")
            except sqlite3.Error as e:
                logger.warning(f"Could not set WAL mode for session: {e}")

        # FIX #3: DEVICE SYNC - передаём device параметры
        client = TelegramClient(
            str(self.account.session_path.with_suffix('')),  # Без .session
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

        # FIX-006: Timeout на connect() чтобы не зависать навечно
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("Telethon connect timeout after 30s")

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Session is not authorized. Cannot proceed with QR login.")

        # Получаем инфо о текущем пользователе (без логирования sensitive data)
        me = await client.get_me()
        logger.info(f"Connected as: {me.first_name} (ID: {me.id})")
        logger.debug(f"Device: {device.device_model} / {device.system_version}")

        return client

    async def _verify_telethon_session(self, client: TelegramClient) -> bool:
        """
        FIX #2: Проверяет что Telethon сессия всё ещё работает после авторизации браузера.
        """
        try:
            me = await client.get_me()
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
            result = await client(SetAuthorizationTTLRequest(
                authorization_ttl_days=self.AUTH_TTL_DAYS
            ))
            logger.info("Authorization TTL set to %d days", self.AUTH_TTL_DAYS)
            return bool(result)
        except Exception as e:
            logger.warning("Failed to set authorization TTL: %s", e)
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
                '[data-peer-id], '                    # Chat items with peer ID
                '.chatlist-chat, '                    # Individual chats
                'li.chatlist-chat, '                  # Chat list items
                '.dialog, '                           # Dialog elements
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
                '.tabs-tab, '                        # Tab navigation
                '.sidebar, '                          # Sidebar
                '#column-left, '                      # Left column
                '.chats-container, '                  # Chats container
                '.folders-tabs, '                     # Folder tabs
                '[class*="LeftColumn"], '             # Left column variations
                '[class*="ChatFolders"]'              # Chat folders
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
                '.avatar-like-icon, '                # User avatar
                '[class*="Avatar"], '                 # Avatar component
                '.profile-photo, '                    # Profile photo
                '.menu-toggle'                        # Menu toggle (only in authorized state)
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
            if ('@' in current_url) or ('/k/#-' in current_url) or ('/a/#-' in current_url):
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
                page_text = await page.inner_text('body')
                if 'Enter Your Password' in page_text or 'Two-Step Verification' in page_text:
                    logger.debug("2FA required: password text found")
                    return "2fa_required"
            except Exception:
                pass

            # Проверяем QR код (только если это не 2FA страница и не authorized)
            qr_canvas = await page.query_selector('canvas')
            if qr_canvas:
                try:
                    is_visible = await qr_canvas.is_visible()
                    if is_visible:
                        # Дополнительно проверяем что это QR login page по тексту
                        try:
                            qr_text = await page.inner_text('body')
                            # QR login page has specific text
                            qr_indicators = ['scan', 'qr', 'log in', 'phone', 'quick']
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
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            logger.debug(f"Error checking page state: {error_msg}")
            return "unknown"

    async def _wait_for_qr(self, page, timeout: int = QR_WAIT_TIMEOUT) -> Optional[bytes]:
        """
        Ждёт появления QR-кода и извлекает токен.

        Использует несколько методов:
        1. jsQR library injected in browser (most reliable)
        2. JS extraction - напрямую из DOM/переменных страницы
        3. Canvas screenshot + OpenCV decoding

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

                if qr_token_direct and 'tg://login?token=' in str(qr_token_direct):
                    logger.info("Token found directly in app state")
                    token_bytes = extract_token_from_tg_url(qr_token_direct)
                    if token_bytes:
                        return token_bytes

                # Метод 0b: Инжектируем jsQR и декодируем canvas прямо в браузере
                # Это работает лучше чем внешние декодеры для стилизованных QR Telegram
                # Загружаем jsQR из локального файла (CDN блокируется CSP)
                if not hasattr(self, '_jsqr_injected') or not self._jsqr_injected:
                    try:
                        import os
                        jsqr_path = os.path.join(
                            os.path.dirname(__file__), '..', 'node_modules', 'jsqr', 'dist', 'jsQR.js'
                        )
                        if os.path.exists(jsqr_path):
                            await page.add_script_tag(path=jsqr_path)
                            self._jsqr_injected = True
                            logger.debug("jsQR injected into page")
                        else:
                            logger.debug(f"jsQR not found at {jsqr_path}")
                    except Exception as e:
                        logger.debug(f"jsQR injection error: {e}")

                qr_from_browser = await page.evaluate("""
                    () => {
                        const canvas = document.querySelector('canvas');
                        if (!canvas) return null;

                        const ctx = canvas.getContext('2d');
                        if (!ctx) return null;

                        if (typeof jsQR !== 'function') {
                            return { error: 'jsQR not available' };
                        }

                        // Получаем данные canvas
                        const width = canvas.width;
                        const height = canvas.height;
                        const imageData = ctx.getImageData(0, 0, width, height);

                        // Декодируем с jsQR (attemptBoth пробует и нормальную и инвертированную версию)
                        const code = jsQR(imageData.data, width, height, {
                            inversionAttempts: 'attemptBoth'
                        });

                        if (code && code.data) {
                            return { qrData: code.data };
                        }

                        return { error: 'QR not decoded', width, height };
                    }
                """)

                if qr_from_browser:
                    if qr_from_browser.get('qrData'):
                        qr_data = qr_from_browser['qrData']
                        logger.info(f"Token decoded via browser jsQR")
                        if 'tg://login?token=' in qr_data:
                            token_bytes = extract_token_from_tg_url(qr_data)
                            if token_bytes:
                                return token_bytes
                    elif qr_from_browser.get('error'):
                        logger.debug(f"Browser jsQR: {qr_from_browser.get('error')}")

                # Fallback: Получить canvas data для внешнего декодирования
                qr_from_canvas = await page.evaluate("""
                    () => {
                        const canvas = document.querySelector('canvas');
                        if (!canvas) return null;

                        const ctx = canvas.getContext('2d');
                        if (!ctx) return null;

                        return {
                            dataUrl: canvas.toDataURL('image/png'),
                            width: canvas.width,
                            height: canvas.height
                        };
                    }
                """)

                if qr_from_canvas and qr_from_canvas.get('dataUrl'):
                    # Decode the canvas data URL externally
                    try:
                        canvas_data = qr_from_canvas['dataUrl'].split(',')[1]
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

                if token_from_js and 'tg://login?token=' in str(token_from_js):
                    logger.info("Token found via JS extraction")
                    # Извлекаем token bytes напрямую
                    token_bytes = extract_token_from_tg_url(token_from_js)
                    if token_bytes:
                        return token_bytes

                # Метод 2: Ищем QR canvas и делаем скриншот
                qr_element = await page.query_selector('canvas')

                if qr_element:
                    # Ждём полной отрисовки QR
                    await asyncio.sleep(2)

                    # Делаем скриншот всей страницы
                    screenshot = await page.screenshot(full_page=False)
                    return screenshot

            except Exception as e:
                # Игнорируем ошибки селектора, продолжаем попытки
                if attempt == timeout - 1:
                    logger.warning(f"QR search error: {e}")

            await asyncio.sleep(1)

            if attempt % 5 == 0 and attempt > 0:
                logger.debug(f"Still waiting for QR... ({attempt}s)")

        return None

    def _is_screenshot_bytes(self, data: bytes) -> bool:
        """
        FIX-001: Проверяет является ли data изображением (screenshot).

        Проверяет magic bytes для PNG, JPEG, GIF форматов.
        """
        if len(data) < 8:
            return False

        # PNG magic: 89 50 4E 47 0D 0A 1A 0A
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return True

        # JPEG magic: FF D8 FF
        if data[:3] == b'\xff\xd8\xff':
            return True

        # GIF magic: GIF87a or GIF89a
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return True

        return False

    def _is_tg_url_token(self, data: bytes) -> bool:
        """
        FIX-001: Проверяет является ли data tg://login?token=... URL.
        """
        try:
            text = data.decode('utf-8', errors='strict')
            if 'tg://login?token=' in text:
                token_part = text.split('token=')[1].split('&')[0]
                if 20 <= len(token_part) <= 100:
                    return True
            return False
        except (UnicodeDecodeError, IndexError):
            return False

    async def _extract_qr_token_with_retry(self, page) -> Optional[bytes]:
        """
        FIX #4: QR RETRY - извлекает QR токен с повторными попытками.

        Поддерживает два варианта ответа от _wait_for_qr:
        1. Token bytes - если JS успешно извлёк токен (tg://login?token=...)
        2. Screenshot bytes - для декодирования через pyzbar

        FIX-001: Используем проверку формата вместо размера для определения типа.
        """
        profile_name = self.account.name.replace(' ', '_').replace('/', '_')

        for retry in range(self.QR_MAX_RETRIES):
            if retry > 0:
                logger.info(f"QR retry {retry + 1}/{self.QR_MAX_RETRIES}...")
                # Сбрасываем флаг jsQR при reload (скрипт теряется)
                if hasattr(self, '_jsqr_injected'):
                    delattr(self, '_jsqr_injected')
                # Обновляем страницу для нового QR
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    logger.warning(f"Page reload warning: {e}")
                await asyncio.sleep(self.QR_RETRY_DELAY)

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
                token = extract_token_from_tg_url(result.decode('utf-8'))
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
                    timestamp = datetime.now().strftime('%H%M%S')
                    debug_path = Path("profiles") / f"debug_qr_{profile_name}_{timestamp}_r{retry}.png"
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(debug_path, 'wb') as f:
                        f.write(result)
                    logger.info(f"Debug screenshot saved: {debug_path}")

            else:
                # Это уже raw token bytes (например, после canvas decode)
                logger.info(f"Token already decoded ({len(result)} bytes)")
                return result

        return None

    async def _accept_token(self, client: TelegramClient, token: bytes) -> bool:
        """
        Отправляет acceptLoginToken для авторизации браузера с FloodWaitError handling.

        Реализует:
        - FloodWaitError handling с ожиданием указанного времени
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
                return True

            except FloodWaitError as e:
                wait_time = e.seconds
                logger.warning(
                    f"FloodWaitError: must wait {wait_time}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                # Если слишком долго ждать - прерываем
                if wait_time > 3600:  # 1 час максимум
                    logger.error(f"Flood wait {wait_time}s too long (>1h), aborting")
                    return False

                # Ждём указанное время + небольшой jitter
                jitter = random.uniform(1, 5)
                logger.info(f"Waiting {wait_time + jitter:.1f}s before retry...")
                await asyncio.sleep(wait_time + jitter)

            except Exception as e:
                # Exponential backoff для других ошибок
                delay = base_delay * (2 ** attempt) + random.uniform(0, 3)
                logger.warning(
                    f"Error accepting token: {e}, "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )

                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)

        logger.error("Failed to accept token after all retries")
        return False

    async def _wait_for_auth_complete(self, page, timeout: int = AUTH_WAIT_TIMEOUT) -> Tuple[bool, bool]:
        """
        Ждёт завершения авторизации в браузере.
        Returns: (success, required_2fa)
        """
        logger.info(f"Waiting for browser authorization (timeout: {timeout}s)...")

        for i in range(timeout):
            current_url = page.url

            # Проверяем успешную авторизацию - ищем элементы главной страницы
            chat_list = await page.query_selector('.chatlist, .chat-list, [class*="ChatList"], .folders-tabs')
            search_input = await page.query_selector('input[placeholder="Search"], .input-search')

            if chat_list or search_input:
                logger.info("Authorization successful (chat list found)")
                return (True, False)

            # Также проверяем URL
            if '/k/#' in current_url and 'auth' not in current_url.lower():
                login_form = await page.query_selector('.auth-form, [class*="auth-page"]')
                if not login_form:
                    logger.info("Authorization successful (URL check)")
                    return (True, False)

            # Проверяем 2FA форму
            password_input = await page.query_selector(
                'input[type="password"], '
                '[class*="password"], '
                '[placeholder*="Password"], '
                '[placeholder*="пароль"]'
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
            'input[name="notsearch_password"]',          # Telegram Web K by name
            'input[type="password"]:not(.stealthy)',     # Exclude hidden inputs
            '.input-field-input[type="password"]',
            'input[placeholder="Password"]',
            'input[placeholder*="assword"]',
            'input[autocomplete="current-password"]',
            # Telegram Web A / mobile
            'input.PasswordForm__input',
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

                        if is_visible and is_enabled and box and box['width'] > 0:
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
            timestamp = datetime.now().strftime('%H%M%S')
            debug_path = Path("profiles") / f"debug_2fa_notfound_{timestamp}.png"
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
            await page.keyboard.press('Enter')
            logger.debug("Pressed Enter to submit")

        except Exception as e:
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            logger.error(f"Password input failed: {error_msg}")
            # Debug screenshot
            timestamp = datetime.now().strftime('%H%M%S')
            debug_path = Path("profiles") / f"debug_2fa_error_{timestamp}.png"
            await page.screenshot(path=str(debug_path))
            return False

        logger.info("Password submitted, waiting for response...")

        # Ждём результата - увеличено время для медленных соединений
        # Также ждём пока кнопка перестанет показывать "PLEASE WAIT..."
        for wait_attempt in range(15):  # До 15 секунд
            await asyncio.sleep(1)

            # Проверяем не исчезла ли форма пароля (успешный вход)
            password_still_visible = await page.query_selector('input[type="password"]')
            if not password_still_visible:
                logger.info("Password form disappeared - likely successful")
                break

            # Check for INCORRECT PASSWORD on submit button
            incorrect_btn = await page.query_selector('button:has-text("INCORRECT PASSWORD"), button:has-text("INCORRECT")')
            if incorrect_btn:
                logger.error("2FA error: INCORRECT PASSWORD")
                return False

            # Проверяем loading state
            loading_btn = await page.query_selector('button:has-text("PLEASE WAIT"), button:has-text("Loading")')
            if not loading_btn:
                # Кнопка не в loading state - можем проверить результат
                break

        # Debug screenshot
        timestamp = datetime.now().strftime('%H%M%S')
        debug_path = Path("profiles") / f"debug_2fa_after_{timestamp}.png"
        await page.screenshot(path=str(debug_path))
        logger.debug(f"Debug screenshot: {debug_path}")

        # Проверяем ошибки
        error_selectors = [
            '[class*="error"]',
            '.error',
            '.input-field-error',
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

    async def authorize(
        self,
        password_2fa: Optional[str] = None,
        headless: bool = False
    ) -> AuthResult:
        """
        Выполняет полный цикл QR-авторизации.

        Args:
            password_2fa: Пароль 2FA если известен
            headless: Headless режим браузера

        Returns:
            AuthResult с результатом
        """
        # FIX #3: DEVICE SYNC - передаём device config в browser manager
        profile = self.browser_manager.get_profile(
            self.account.name,
            self.account.proxy
        )

        logger.info("=" * 60)
        logger.info("TELEGRAM WEB AUTHORIZATION")
        logger.info(f"Account: {self.account.name}")
        logger.info(f"Profile: {profile.name}")
        logger.debug(f"Device: {self.account.device.device_model} / {self.account.device.system_version}")
        logger.info("=" * 60)

        client = None
        browser_ctx = None

        try:
            # 1. Подключаем Telethon client
            logger.info("[1/6] Connecting Telethon client...")
            client = await self._create_telethon_client()
            self._client = client

            # 2. Запускаем браузер с синхронизированной ОС
            logger.info("[2/6] Launching browser...")
            # FIX #3: Передаём os_list для консистентности
            browser_extra_args = {
                "os": self.account.device.browser_os_list,
            }
            browser_ctx = await self.browser_manager.launch(
                profile,
                headless=headless,
                extra_args=browser_extra_args
            )
            page = await browser_ctx.new_page()

            # Устанавливаем viewport для корректного отображения QR
            await page.set_viewport_size({"width": 1280, "height": 800})

            # 3. Открываем Telegram Web
            logger.info("[3/6] Opening Telegram Web...")
            try:
                await page.goto(self.TELEGRAM_WEB_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"Page load warning: {e}")

            # Ждём загрузки страницы (до 15 секунд)
            logger.debug("Waiting for page to load...")
            for i in range(15):
                await asyncio.sleep(1)
                # Проверяем что страница загрузилась
                body = await page.query_selector('body')
                if body:
                    html = await page.content()
                    if 'telegram' in html.lower() or 'qr' in html.lower() or 'password' in html.lower():
                        logger.debug(f"Page loaded after {i+1}s")
                        break

            await asyncio.sleep(3)  # Дополнительная пауза для рендеринга

            # Проверяем текущее состояние страницы с повторными попытками
            # Возможно профиль уже авторизован или требует 2FA
            current_state = "unknown"
            for state_check in range(10):
                current_state = await self._check_page_state(page)
                if current_state != "unknown" and current_state != "loading":
                    break
                await asyncio.sleep(1)
                if state_check % 3 == 0 and state_check > 0:
                    logger.debug(f"Checking page state... ({state_check}s)")

            logger.info(f"Current page state: {current_state}")

            if current_state == "authorized":
                logger.info("Already authorized! Skipping QR login.")
                await self._set_authorization_ttl(client)
                return AuthResult(
                    success=True,
                    profile_name=profile.name,
                    required_2fa=False,
                    telethon_alive=await self._verify_telethon_session(client)
                )

            if current_state == "2fa_required":
                logger.info("2FA required (session from previous run)")
                if password_2fa:
                    fa_success = await self._handle_2fa(page, password_2fa)
                    if fa_success:
                        success, _ = await self._wait_for_auth_complete(page, timeout=30)
                        if success:
                            await self._set_authorization_ttl(client)
                            return AuthResult(
                                success=True,
                                profile_name=profile.name,
                                required_2fa=True,
                                telethon_alive=await self._verify_telethon_session(client)
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
                    return AuthResult(
                        success=success,
                        profile_name=profile.name,
                        required_2fa=True,
                        error=None if success else "2FA password required",
                        telethon_alive=await self._verify_telethon_session(client)
                    )

            # 4. Ждём и декодируем QR (FIX #4: с retry)
            logger.info("[4/6] Extracting QR token...")
            token = await self._extract_qr_token_with_retry(page)

            if not token:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error=f"Failed to extract QR token after {self.QR_MAX_RETRIES} attempts"
                )

            # 5. Подтверждаем токен через Telethon
            logger.info("[5/6] Accepting token via Telethon...")
            accepted = await self._accept_token(client, token)

            if not accepted:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error="Failed to accept login token"
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
            logger.info("[6/6] Verifying Telethon session...")
            telethon_alive = await self._verify_telethon_session(client)

            if success:
                await self._set_authorization_ttl(client)

                # Получаем инфо о пользователе из браузера
                user_info = await self._get_browser_user_info(page)

                return AuthResult(
                    success=True,
                    profile_name=profile.name,
                    required_2fa=need_2fa,
                    user_info=user_info,
                    telethon_alive=telethon_alive
                )
            else:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error="Authorization did not complete",
                    required_2fa=need_2fa,
                    telethon_alive=telethon_alive
                )

        except Exception as e:
            logger.error("Authorization failed for '%s': %s", profile.name, e, exc_info=True)
            return AuthResult(
                success=False,
                profile_name=profile.name,
                error=str(e)
            )

        finally:
            # FIX #5: ERROR RECOVERY - гарантированный cleanup
            if client:
                try:
                    await client.disconnect()
                    logger.debug("Telethon client disconnected")
                except Exception as e:
                    logger.warning(f"Error disconnecting client: {e}")

            if browser_ctx:
                try:
                    await browser_ctx.close()
                except Exception as e:
                    logger.warning(f"Error closing browser: {e}")

    async def _get_browser_user_info(self, page) -> Optional[dict]:
        """Извлекает информацию о пользователе из браузера"""
        try:
            # Пробуем получить имя из UI
            name_element = await page.query_selector(
                '.user-title, '
                '[class*="peer-title"], '
                '.profile-name'
            )
            if name_element:
                name = await name_element.inner_text()
                return {"name": name.strip()}
        except (TimeoutError, AttributeError) as e:
            # UI элемент не найден или не доступен
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
    Результат в диапазоне [MIN_COOLDOWN, MAX_COOLDOWN].

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

    # Ограничиваем диапазон
    return max(MIN_COOLDOWN, min(delay, MAX_COOLDOWN))


async def migrate_account(
    account_dir: Path,
    password_2fa: Optional[str] = None,
    headless: bool = False,
    proxy_override: Optional[str] = None,
    browser_manager: Optional[BrowserManager] = None,
) -> AuthResult:
    """
    Мигрирует один аккаунт из session в browser profile.

    Args:
        account_dir: Директория с session, api.json, ___config.json
        password_2fa: Пароль 2FA
        headless: Headless режим
        proxy_override: Прокси строка из БД (перезаписывает ___config.json)
        browser_manager: Shared BrowserManager instance (creates new if None).

    Returns:
        AuthResult
    """
    account = AccountConfig.load(account_dir)
    if proxy_override is not None:
        account.proxy = proxy_override
    auth = TelegramAuth(account, browser_manager=browser_manager)
    return await auth.authorize(password_2fa=password_2fa, headless=headless)


async def migrate_accounts_batch(
    account_dirs: list[Path],
    password_2fa: Optional[str] = None,
    headless: bool = False,
    cooldown: int = DEFAULT_ACCOUNT_COOLDOWN
) -> list[AuthResult]:
    """
    FIX #6: Мигрирует несколько аккаунтов с cooldown между ними.

    Args:
        account_dirs: Список директорий аккаунтов
        password_2fa: Общий 2FA пароль (если одинаковый)
        headless: Headless режим
        cooldown: Секунды между аккаунтами (default 45)

    Returns:
        Список AuthResult
    """
    results = []

    for i, account_dir in enumerate(account_dirs):
        logger.info("=" * 60)
        logger.info(f"ACCOUNT {i + 1}/{len(account_dirs)}: {account_dir.name}")
        logger.info("=" * 60)

        result = await migrate_account(
            account_dir=account_dir,
            password_2fa=password_2fa,
            headless=headless
        )
        results.append(result)

        # FIX #6: Randomized cooldown между аккаунтами (кроме последнего)
        if i < len(account_dirs) - 1:
            jittered_cooldown = get_randomized_cooldown(cooldown)
            logger.info(f"Cooldown {jittered_cooldown:.1f}s before next account (base: {cooldown}s)...")
            await asyncio.sleep(jittered_cooldown)

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

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0
    ):
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
        self._last_failure_time = time.time()

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

    def can_proceed(self) -> bool:
        """
        Check if operations can proceed.

        Returns True if:
        - Circuit is closed (normal operation)
        - Circuit is open but reset_timeout has elapsed (half-open)
        """
        if not self._is_open:
            return True

        import time
        elapsed = time.time() - self._last_failure_time

        if elapsed >= self._reset_timeout:
            logger.info(
                f"Circuit breaker: reset timeout elapsed "
                f"({elapsed:.1f}s >= {self._reset_timeout}s), allowing retry"
            )
            # Move to half-open state - allow one request
            return True

        return False

    def time_until_reset(self) -> float:
        """Seconds until circuit breaker resets (0 if closed)"""
        if not self._is_open:
            return 0.0

        import time
        elapsed = time.time() - self._last_failure_time
        remaining = self._reset_timeout - elapsed
        return max(0.0, remaining)

    def reset(self) -> None:
        """Manually reset the circuit breaker"""
        self._consecutive_failures = 0
        self._is_open = False
        self._last_failure_time = 0.0


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
        circuit_breaker: Optional[CircuitBreaker] = None
    ):
        self.max_concurrent = max_concurrent
        self.cooldown = cooldown
        self.resource_monitor = resource_monitor
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._shutdown_requested = False
        self._active_tasks: Set[asyncio.Task[Any]] = set()
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
        password_2fa: Optional[str] = None,
        headless: bool = False,
        on_progress: Optional[ProgressCallback] = None
    ) -> list[AuthResult]:
        """
        Run parallel migration with shutdown support.

        Returns results for completed accounts (may be partial on shutdown).
        """
        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: dict[int, AuthResult] = {}
        lock = asyncio.Lock()
        self._total = len(account_dirs)
        self._completed = 0
        self._shutdown_requested = False

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
                                success=False,
                                profile_name=account_dir.name,
                                error="Timeout waiting for resources"
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
                        result = await migrate_account(
                            account_dir=account_dir,
                            password_2fa=password_2fa,
                            headless=headless
                        )
                except asyncio.TimeoutError:
                    logger.warning(f"Task timeout after {TASK_TIMEOUT}s for {account_dir.name}")
                    result = AuthResult(
                        success=False,
                        profile_name=account_dir.name,
                        error=f"Task timeout after {TASK_TIMEOUT}s"
                    )
                except Exception as e:
                    result = AuthResult(
                        success=False,
                        profile_name=account_dir.name,
                        error=str(e)
                    )

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
                            on_progress(self._completed, self._total, result)
                        except Exception as e:
                            logger.warning(f"Progress callback error: {e}")

                return result

        # Create and track tasks
        tasks = []
        for i, account_dir in enumerate(account_dirs):
            if self._shutdown_requested:
                break

            task = asyncio.create_task(migrate_one(i, account_dir))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            tasks.append(task)

            # Rate limiting with jitter to avoid pattern detection
            if i < len(account_dirs) - 1 and self.cooldown > 0:
                jittered = get_randomized_cooldown(self.cooldown)
                await asyncio.sleep(jittered)

        # Wait for all started tasks with batch timeout
        BATCH_TIMEOUT = 3600  # 1 hour for entire batch

        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=BATCH_TIMEOUT,
                return_when=asyncio.ALL_COMPLETED
            )

            # Cancel any stuck tasks that exceeded batch timeout
            if pending:
                logger.warning(f"Batch timeout: cancelling {len(pending)} stuck tasks")
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        # Return results in order (only completed ones)
        ordered_results = []
        for i in range(len(account_dirs)):
            if i in results:
                ordered_results.append(results[i])
            elif self._shutdown_requested:
                # Mark as skipped due to shutdown
                ordered_results.append(AuthResult(
                    success=False,
                    profile_name=account_dirs[i].name,
                    error="Skipped due to shutdown"
                ))

        return ordered_results


async def migrate_accounts_parallel(
    account_dirs: list[Path],
    password_2fa: Optional[str] = None,
    headless: bool = False,
    max_concurrent: int = 10,
    cooldown: float = 5.0,
    on_progress: Optional[ProgressCallback] = None
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
    semaphore = asyncio.Semaphore(max_concurrent)
    results: dict[int, AuthResult] = {}
    completed = 0
    total = len(account_dirs)
    lock = asyncio.Lock()

    async def migrate_with_semaphore(index: int, account_dir: Path):
        nonlocal completed
        async with semaphore:
            try:
                result = await migrate_account(
                    account_dir=account_dir,
                    password_2fa=password_2fa,
                    headless=headless
                )
            except Exception as e:
                result = AuthResult(
                    success=False,
                    profile_name=account_dir.name,
                    error=str(e)
                )

            async with lock:
                results[index] = result
                completed += 1
                if on_progress:
                    try:
                        on_progress(completed, total, result)
                    except Exception as e:
                        logger.warning(f"Progress callback error: {e}")

            return result

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

    # Return results in original order
    return [results[i] for i in range(len(account_dirs))]


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

    result = await migrate_account(
        account_dir=account_path,
        password_2fa=args.password,
        headless=args.headless
    )

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
