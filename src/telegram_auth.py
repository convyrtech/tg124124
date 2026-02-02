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
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Callable

# QR decoding - use OpenCV (works on Windows without extra DLLs)
try:
    import cv2
    import numpy as np
    from PIL import Image
except ImportError:
    print("ERROR: opencv-python/Pillow not installed. Run: pip install opencv-python Pillow")
    exit(1)

# pyzbar is optional - lazy load to avoid DLL errors on Windows
pyzbar = None
def _get_pyzbar():
    global pyzbar
    if pyzbar is None:
        try:
            from pyzbar import pyzbar as _pyzbar
            pyzbar = _pyzbar
        except (ImportError, OSError, FileNotFoundError):
            pass  # pyzbar not available, will use OpenCV only
    return pyzbar

# Telethon
try:
    from telethon import TelegramClient
    from telethon.sessions import SQLiteSession
    from telethon.tl.functions.auth import AcceptLoginTokenRequest
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon")
    exit(1)

from .browser_manager import BrowserManager, BrowserProfile, BrowserContext


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
            except json.JSONDecodeError:
                # Config опциональный, игнорируем ошибки парсинга
                pass

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
    user_info: Optional[dict] = None
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

        os.unlink(temp_path)

        if result.returncode == 0 and result.stdout.strip():
            data = result.stdout.strip()
            if data != 'QR_NOT_FOUND' and 'tg://login?token=' in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    print(f"[QR] Decoded with jsQR (Node.js)")
                    return token_bytes
    except Exception as e:
        print(f"[QR] jsQR error: {e}")

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
                        print(f"[QR] Decoded with QRCodeDetectorAruco")
                        return token_bytes
    except Exception as e:
        print(f"[QR] QRCodeDetectorAruco error: {e}")

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
        except Exception:
            pass
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
    except Exception:
        pass

    # 4. Инвертированный grayscale
    try:
        inverted_gray = ImageOps.invert(gray)
        variants.append(("inverted_gray", inverted_gray))
    except Exception:
        pass

    # 5. Высококонтрастная версия
    try:
        enhancer = ImageEnhance.Contrast(gray)
        high_contrast = enhancer.enhance(2.0)
        variants.append(("high_contrast", high_contrast))
        variants.append(("high_contrast_inv", ImageOps.invert(high_contrast)))
    except Exception:
        pass

    # 6. Thresholding (бинаризация)
    try:
        threshold = gray.point(lambda x: 255 if x > 128 else 0, 'L')
        variants.append(("threshold", threshold))
        threshold_inv = gray.point(lambda x: 0 if x > 128 else 255, 'L')
        variants.append(("threshold_inv", threshold_inv))
    except Exception:
        pass

    for name, img_variant in variants:
        # Try OpenCV first (more reliable on Windows)
        try:
            cv_img = pil_to_cv2(img_variant)
            data = decode_with_opencv(cv_img)
            if data and 'tg://login?token=' in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    print(f"[QR] Decoded with OpenCV using {name}")
                    return token_bytes
        except Exception:
            pass

        # Fallback to pyzbar if available
        try:
            data = decode_with_pyzbar(img_variant)
            if data and 'tg://login?token=' in data:
                token_bytes = extract_token(data)
                if token_bytes:
                    print(f"[QR] Decoded with pyzbar using {name}")
                    return token_bytes
        except Exception:
            pass

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
        print(f"[QR] Error extracting token from URL: {e}")
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
    QR_MAX_RETRIES = 3  # FIX #4: QR retry
    QR_RETRY_DELAY = 5  # секунд между retry

    def __init__(self, account: AccountConfig, browser_manager: Optional[BrowserManager] = None):
        self.account = account
        self.browser_manager = browser_manager or BrowserManager()
        self._client: Optional[TelegramClient] = None

    async def _create_telethon_client(self) -> TelegramClient:
        """Создаёт Telethon client из существующей сессии с синхронизированным device"""
        proxy = parse_telethon_proxy(self.account.proxy)
        device = self.account.device

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
        )

        await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError("Session is not authorized. Cannot proceed with QR login.")

        # Получаем инфо о текущем пользователе (без логирования sensitive data)
        me = await client.get_me()
        print(f"[TelegramAuth] Connected as: {me.first_name} (ID: {me.id})")
        print(f"[TelegramAuth] Device: {device.device_model} / {device.system_version}")

        return client

    async def _verify_telethon_session(self, client: TelegramClient) -> bool:
        """
        FIX #2: Проверяет что Telethon сессия всё ещё работает после авторизации браузера.
        """
        try:
            me = await client.get_me()
            if me:
                print(f"[TelegramAuth] Session verified: {me.first_name} still authorized")
                return True
        except Exception as e:
            print(f"[TelegramAuth] Session verification failed: {e}")
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

            # Проверяем авторизован ли пользователь (в чатах)
            if '/k/#' in current_url or '/a/#' in current_url:
                # Дополнительно проверяем что это не страница логина
                chat_list = await page.query_selector('[class*="chat-list"], [class*="dialogs"]')
                if chat_list:
                    is_visible = await chat_list.is_visible()
                    if is_visible:
                        return "authorized"

            # ВАЖНО: Проверяем 2FA форму РАНЬШЕ чем QR (на странице 2FA тоже может быть canvas)
            password_input = await page.query_selector('input[type="password"]')
            if password_input:
                try:
                    is_visible = await password_input.is_visible()
                    if is_visible:
                        return "2fa_required"
                except Exception:
                    pass

            # Проверяем текст "Enter Your Password" на странице
            page_text = await page.inner_text('body')
            if 'Enter Your Password' in page_text or 'password' in page_text.lower():
                return "2fa_required"

            # Проверяем QR код (только если это не 2FA страница)
            qr_canvas = await page.query_selector('canvas')
            if qr_canvas:
                try:
                    is_visible = await qr_canvas.is_visible()
                    if is_visible:
                        # Дополнительно проверяем что это QR login page
                        qr_text = await page.inner_text('body')
                        if 'scan' in qr_text.lower() or 'qr' in qr_text.lower() or 'log in' in qr_text.lower():
                            return "qr_login"
                except Exception:
                    pass

            # Проверяем индикатор загрузки
            spinner = await page.query_selector('[class*="spinner"], [class*="loading"]')
            if spinner:
                try:
                    is_visible = await spinner.is_visible()
                    if is_visible:
                        return "loading"
                except Exception:
                    pass

            return "unknown"

        except Exception as e:
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            print(f"[TelegramAuth] Error checking page state: {error_msg}")
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
        print(f"[TelegramAuth] Waiting for QR code (timeout: {timeout}s)...")

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
                    print(f"[TelegramAuth] Token found directly in app state!")
                    token_bytes = extract_token_from_tg_url(qr_token_direct)
                    if token_bytes:
                        return token_bytes

                # Метод 0b: Получить canvas data для декодирования
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
                            print(f"[TelegramAuth] Token decoded from canvas!")
                            return token
                    except Exception as e:
                        pass  # Continue to other methods

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
                    print(f"[TelegramAuth] Token found via JS extraction!")
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
                    print(f"[TelegramAuth] QR search error: {e}")

            await asyncio.sleep(1)

            if attempt % 5 == 0 and attempt > 0:
                print(f"[TelegramAuth] Still waiting for QR... ({attempt}s)")

        return None

    async def _extract_qr_token_with_retry(self, page) -> Optional[bytes]:
        """
        FIX #4: QR RETRY - извлекает QR токен с повторными попытками.

        Поддерживает два варианта ответа от _wait_for_qr:
        1. Token bytes - если JS успешно извлёк токен
        2. Screenshot bytes - для декодирования через pyzbar
        """
        for retry in range(self.QR_MAX_RETRIES):
            if retry > 0:
                print(f"\n[TelegramAuth] QR retry {retry + 1}/{self.QR_MAX_RETRIES}...")
                # Обновляем страницу для нового QR
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"[TelegramAuth] Page reload warning: {e}")
                await asyncio.sleep(self.QR_RETRY_DELAY)

            result = await self._wait_for_qr(page)

            if not result:
                print(f"[TelegramAuth] QR not found on page (attempt {retry + 1})")
                continue

            # Проверяем что вернулось - token bytes или screenshot
            # Token обычно < 100 байт, screenshot > 10KB
            if len(result) < 500:
                # Это уже готовый token bytes
                print(f"[TelegramAuth] Token extracted via JS ({len(result)} bytes)")
                return result
            else:
                # Это screenshot, нужно декодировать
                token = decode_qr_from_screenshot(result)

                if token:
                    print(f"[TelegramAuth] Token extracted from screenshot ({len(token)} bytes)")
                    return token
                else:
                    print(f"[TelegramAuth] Failed to decode QR from screenshot (attempt {retry + 1})")
                    # Сохраняем debug скриншот
                    debug_path = Path("profiles") / f"debug_qr_retry_{retry}.png"
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(debug_path, 'wb') as f:
                        f.write(result)
                    print(f"[TelegramAuth] Debug screenshot saved: {debug_path}")

        return None

    async def _accept_token(self, client: TelegramClient, token: bytes) -> bool:
        """Отправляет acceptLoginToken для авторизации браузера"""
        try:
            print(f"[TelegramAuth] Accepting login token...")
            result = await client(AcceptLoginTokenRequest(token=token))
            print(f"[TelegramAuth] Token accepted! Authorization: {type(result).__name__}")
            return True
        except Exception as e:
            print(f"[TelegramAuth] Error accepting token: {e}")
            return False

    async def _wait_for_auth_complete(self, page, timeout: int = AUTH_WAIT_TIMEOUT) -> Tuple[bool, bool]:
        """
        Ждёт завершения авторизации в браузере.
        Returns: (success, required_2fa)
        """
        print(f"[TelegramAuth] Waiting for browser authorization (timeout: {timeout}s)...")

        for i in range(timeout):
            current_url = page.url

            # Проверяем успешную авторизацию - ищем элементы главной страницы
            chat_list = await page.query_selector('.chatlist, .chat-list, [class*="ChatList"], .folders-tabs')
            search_input = await page.query_selector('input[placeholder="Search"], .input-search')

            if chat_list or search_input:
                print(f"[TelegramAuth] Authorization successful! (chat list found)")
                return (True, False)

            # Также проверяем URL
            if '/k/#' in current_url and 'auth' not in current_url.lower():
                login_form = await page.query_selector('.auth-form, [class*="auth-page"]')
                if not login_form:
                    print(f"[TelegramAuth] Authorization successful! (URL check)")
                    return (True, False)

            # Проверяем 2FA форму
            password_input = await page.query_selector(
                'input[type="password"], '
                '[class*="password"], '
                '[placeholder*="Password"], '
                '[placeholder*="пароль"]'
            )
            if password_input:
                print(f"[TelegramAuth] 2FA password required!")
                return (False, True)

            # Проверяем ошибки
            error_element = await page.query_selector('[class*="error"], .error-message')
            if error_element:
                error_text = await error_element.inner_text()
                print(f"[TelegramAuth] Error detected: {error_text}")
                return (False, False)

            await asyncio.sleep(1)

            if i % 10 == 0 and i > 0:
                print(f"[TelegramAuth] Still waiting... ({i}s)")

        return (False, False)

    async def _handle_2fa(self, page, password: str) -> bool:
        """Вводит 2FA пароль"""
        print(f"[TelegramAuth] Entering 2FA password...")

        # Ждём появления поля ввода пароля (до 10 секунд)
        password_input = None
        password_selectors = [
            'input[type="password"]',
            'input[placeholder="Password"]',
            'input[placeholder*="assword"]',
            'input[placeholder*="ароль"]',
            '[class*="password"] input',
            '.input-field-input[type="password"]',
            'input.input-field-input',
        ]

        for attempt in range(10):
            for selector in password_selectors:
                try:
                    password_input = await page.query_selector(selector)
                    if password_input:
                        # Проверяем что элемент видим
                        is_visible = await password_input.is_visible()
                        if is_visible:
                            print(f"[TelegramAuth] Found password input with selector: {selector}")
                            break
                        else:
                            password_input = None
                except Exception:
                    continue
            if password_input:
                break
            await asyncio.sleep(1)
            if attempt % 3 == 0 and attempt > 0:
                print(f"[TelegramAuth] Still looking for password field... ({attempt}s)")

        if not password_input:
            # Сохраним скриншот для отладки
            debug_path = Path("profiles") / "debug_2fa_form.png"
            await page.screenshot(path=str(debug_path))
            print(f"[TelegramAuth] Password input not found! Screenshot saved to {debug_path}")
            return False

        try:
            # Получаем координаты поля пароля и кликаем по центру
            box = await password_input.bounding_box()
            if box:
                x = box['x'] + box['width'] / 2
                y = box['y'] + box['height'] / 2
                await page.mouse.click(x, y)
                await asyncio.sleep(0.5)
                print(f"[TelegramAuth] Clicked at ({x}, {y})")

            # Вводим пароль посимвольно
            await page.keyboard.type(password, delay=100)
            await asyncio.sleep(0.5)
            print(f"[TelegramAuth] Password typed ({len(password)} chars)")

            # Нажимаем Enter
            await page.keyboard.press('Enter')
            print("[TelegramAuth] Pressed Enter to submit")

        except Exception as e:
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            print(f"[TelegramAuth] Mouse click + type failed: {error_msg}")

            # Fallback: использеум locator.type
            try:
                await password_input.type(password, delay=100)
                await asyncio.sleep(0.5)
                await page.keyboard.press('Enter')
                print(f"[TelegramAuth] Password entered via locator.type")
            except Exception as e2:
                error_msg2 = str(e2).encode('ascii', 'replace').decode('ascii')
                print(f"[TelegramAuth] Locator.type also failed: {error_msg2}")
                return False

        print("[TelegramAuth] Password submitted, waiting for response...")

        # Ждём результата (даём больше времени)
        await asyncio.sleep(5)

        # Сохраняем скриншот для отладки
        debug_path = Path("profiles") / "debug_after_2fa.png"
        await page.screenshot(path=str(debug_path))
        print(f"[TelegramAuth] Debug screenshot saved: {debug_path}")

        # Проверяем ошибки - Telegram Web K использует разные классы
        error_selectors = [
            '[class*="error"]',
            '.error',
            '.input-field-error',
            '[class*="shake"]',  # Анимация ошибки
        ]
        for selector in error_selectors:
            try:
                error = await page.query_selector(selector)
                if error:
                    is_visible = await error.is_visible()
                    if is_visible:
                        error_text = await error.inner_text()
                        if error_text.strip():
                            print(f"[TelegramAuth] 2FA error detected: {error_text}")
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

        print(f"\n{'='*60}")
        print(f"TELEGRAM WEB AUTHORIZATION")
        print(f"Account: {self.account.name}")
        print(f"Profile: {profile.name}")
        print(f"Device: {self.account.device.device_model} / {self.account.device.system_version}")
        print(f"{'='*60}\n")

        client = None
        browser_ctx = None

        try:
            # 1. Подключаем Telethon client
            print("[1/6] Connecting Telethon client...")
            client = await self._create_telethon_client()
            self._client = client

            # 2. Запускаем браузер с синхронизированной ОС
            print("\n[2/6] Launching browser...")
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
            print("\n[3/6] Opening Telegram Web...")
            try:
                await page.goto(self.TELEGRAM_WEB_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[TelegramAuth] Page load warning: {e}")

            # Ждём загрузки страницы (до 15 секунд)
            print("[TelegramAuth] Waiting for page to load...")
            for i in range(15):
                await asyncio.sleep(1)
                # Проверяем что страница загрузилась
                body = await page.query_selector('body')
                if body:
                    html = await page.content()
                    if 'telegram' in html.lower() or 'qr' in html.lower() or 'password' in html.lower():
                        print(f"[TelegramAuth] Page loaded after {i+1}s")
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
                    print(f"[TelegramAuth] Checking page state... ({state_check}s)")

            print(f"[TelegramAuth] Current page state: {current_state}")

            if current_state == "authorized":
                print("[TelegramAuth] Already authorized! Skipping QR login.")
                return AuthResult(
                    success=True,
                    profile_name=profile.name,
                    required_2fa=False,
                    telethon_alive=await self._verify_telethon_session(client)
                )

            if current_state == "2fa_required":
                print("[TelegramAuth] 2FA required (session from previous run)")
                if password_2fa:
                    fa_success = await self._handle_2fa(page, password_2fa)
                    if fa_success:
                        success, _ = await self._wait_for_auth_complete(page, timeout=30)
                        if success:
                            return AuthResult(
                                success=True,
                                profile_name=profile.name,
                                required_2fa=True,
                                telethon_alive=await self._verify_telethon_session(client)
                            )
                else:
                    print("[TelegramAuth] 2FA password not provided, waiting for manual input...")
                    success, _ = await self._wait_for_auth_complete(page)
                    return AuthResult(
                        success=success,
                        profile_name=profile.name,
                        required_2fa=True,
                        error=None if success else "2FA password required",
                        telethon_alive=await self._verify_telethon_session(client)
                    )

            # 4. Ждём и декодируем QR (FIX #4: с retry)
            print("\n[4/6] Extracting QR token...")
            token = await self._extract_qr_token_with_retry(page)

            if not token:
                return AuthResult(
                    success=False,
                    profile_name=profile.name,
                    error=f"Failed to extract QR token after {self.QR_MAX_RETRIES} attempts"
                )

            # 5. Подтверждаем токен через Telethon
            print("\n[5/6] Accepting token via Telethon...")
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
                print(f"[TelegramAuth] Page reload after accept: {e}")

            # Ждём авторизацию в браузере
            success, need_2fa = await self._wait_for_auth_complete(page)

            # Обрабатываем 2FA
            if need_2fa:
                if password_2fa:
                    print("\n[TelegramAuth] Handling 2FA...")
                    fa_success = await self._handle_2fa(page, password_2fa)

                    if fa_success:
                        # Ждём окончательную авторизацию
                        success, _ = await self._wait_for_auth_complete(page, timeout=30)
                else:
                    print("\n[TelegramAuth] 2FA required but password not provided.")
                    print("[TelegramAuth] Please enter password manually in browser...")

                    # Ждём ручной ввод
                    success, _ = await self._wait_for_auth_complete(page)

            # FIX #2: Проверяем что Telethon сессия жива после авторизации браузера
            print("\n[6/6] Verifying Telethon session...")
            telethon_alive = await self._verify_telethon_session(client)

            if success:
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
            import traceback
            traceback.print_exc()
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
                    print("[TelegramAuth] Telethon client disconnected")
                except Exception as e:
                    print(f"[TelegramAuth] Warning: error disconnecting client: {e}")

            if browser_ctx:
                try:
                    await browser_ctx.close()
                except Exception as e:
                    print(f"[TelegramAuth] Warning: error closing browser: {e}")

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
DEFAULT_ACCOUNT_COOLDOWN = 45  # секунд между аккаунтами


async def migrate_account(
    account_dir: Path,
    password_2fa: Optional[str] = None,
    headless: bool = False
) -> AuthResult:
    """
    Мигрирует один аккаунт из session в browser profile.

    Args:
        account_dir: Директория с session, api.json, ___config.json
        password_2fa: Пароль 2FA
        headless: Headless режим

    Returns:
        AuthResult
    """
    account = AccountConfig.load(account_dir)
    auth = TelegramAuth(account)
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
        print(f"\n{'='*60}")
        print(f"ACCOUNT {i + 1}/{len(account_dirs)}: {account_dir.name}")
        print(f"{'='*60}")

        result = await migrate_account(
            account_dir=account_dir,
            password_2fa=password_2fa,
            headless=headless
        )
        results.append(result)

        # FIX #6: Cooldown между аккаунтами (кроме последнего)
        if i < len(account_dirs) - 1:
            print(f"\n[Batch] Cooldown {cooldown}s before next account...")
            await asyncio.sleep(cooldown)

    return results


# Progress callback type for parallel migration
ProgressCallback = Callable[[int, int, Optional["AuthResult"]], None]


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
                        print(f"[Parallel] Progress callback error: {e}")

            return result

    # Create tasks with staggered start (rate limiting)
    tasks = []
    for i, account_dir in enumerate(account_dirs):
        task = asyncio.create_task(migrate_with_semaphore(i, account_dir))
        tasks.append(task)
        # Stagger task creation to avoid thundering herd
        if i < len(account_dirs) - 1 and cooldown > 0:
            await asyncio.sleep(cooldown)

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
        print(f"Error: Account directory not found: {account_path}")
        exit(1)

    result = await migrate_account(
        account_dir=account_path,
        password_2fa=args.password,
        headless=args.headless
    )

    print(f"\n{'='*60}")
    print("RESULT")
    print(f"{'='*60}")
    print(f"Success: {result.success}")
    print(f"Profile: {result.profile_name}")
    print(f"Telethon alive: {result.telethon_alive}")
    if result.error:
        print(f"Error: {result.error}")
    if result.required_2fa:
        print(f"Required 2FA: Yes")
    if result.user_info:
        print(f"User: {result.user_info}")

    exit(0 if result.success else 1)


if __name__ == "__main__":
    asyncio.run(main())
