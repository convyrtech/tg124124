"""
Fragment.com Authorization Module
Авторизация на fragment.com через существующий Telegram профиль и Telethon сессию.

Принцип работы:
1. Открываем fragment.com в существующем Camoufox профиле
2. Проверяем: уже авторизован на fragment.com?
3. Если нет — нажимаем "Connect Telegram"
4. Вводим номер телефона
5. Telethon перехватывает код подтверждения из сообщения от user 777000
6. Playwright вводит код в browser
7. Профиль авторизован на fragment.com

ВАЖНО: НЕ логировать auth_key, api_hash, phone numbers полностью!
"""
import asyncio
import logging
import random
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from .browser_manager import BrowserManager, BrowserContext
from .telegram_auth import AccountConfig, AuthResult, parse_telethon_proxy

logger = logging.getLogger(__name__)


FRAGMENT_URL = "https://fragment.com"
# Telegram service account that sends login codes
TELEGRAM_SERVICE_USER_ID = 777000

# Timeouts
PAGE_LOAD_TIMEOUT = 60000  # ms
CODE_WAIT_TIMEOUT = 120  # seconds
AUTH_COMPLETE_TIMEOUT = 30  # seconds


def _mask_phone(phone: str) -> str:
    """Маскирует номер телефона для логов: +7999***4567"""
    if not phone or len(phone) < 7:
        return "***"
    return phone[:4] + "***" + phone[-4:]


@dataclass
class FragmentResult:
    """Результат авторизации на Fragment"""
    success: bool
    account_name: str
    already_authorized: bool = False
    error: Optional[str] = None
    telegram_connected: bool = False


class FragmentAuth:
    """
    Авторизация на fragment.com через существующий Telegram профиль.

    Использует:
    - Существующий Camoufox browser profile (тот же что для web.telegram.org)
    - Telethon client для перехвата кода подтверждения
    - Тот же прокси что у аккаунта
    """

    def __init__(
        self,
        account: AccountConfig,
        browser_manager: Optional[BrowserManager] = None,
    ):
        self.account = account
        self.browser_manager = browser_manager or BrowserManager()
        self._client: Optional[TelegramClient] = None
        self._verification_code: Optional[str] = None
        self._code_event: Optional[asyncio.Event] = None  # Created lazily in running loop

    async def _create_telethon_client(self) -> TelegramClient:
        """
        Создаёт Telethon client из существующей сессии.
        Reuses the same logic as TelegramAuth._create_telethon_client.
        """
        proxy = parse_telethon_proxy(self.account.proxy)
        device = self.account.device

        # WAL mode for SQLite session
        session_path = self.account.session_path
        if session_path.exists():
            try:
                conn = sqlite3.connect(str(session_path), timeout=10)
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=10000')
                conn.close()
            except sqlite3.Error as e:
                logger.warning("Could not set WAL mode for session: %s", e)

        client = TelegramClient(
            str(self.account.session_path.with_suffix('')),
            self.account.api_id,
            self.account.api_hash,
            proxy=proxy,
            device_model=device.device_model,
            system_version=device.system_version,
            app_version=device.app_version,
            lang_code=device.lang_code,
            system_lang_code=device.system_lang_code,
        )

        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("Telethon connect timeout after 30s")

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Session is not authorized.")

        me = await client.get_me()
        logger.info("Connected as: %s (ID: %s)", me.first_name, me.id)

        return client

    def _extract_code_from_message(self, text: str) -> Optional[str]:
        """
        Извлекает код подтверждения из сообщения Telegram.

        Telegram Login Widget отправляет сообщение вида:
        "Login code: 12345. Do not give this code to anyone..."
        """
        if not text:
            return None

        # Pattern: "Login code: XXXXX" or "Код входа: XXXXX"
        patterns = [
            r'Login code:\s*(\d{5,6})',
            r'Код входа:\s*(\d{5,6})',
            r'login code[:\s]+(\d{5,6})',
            r'code[:\s]+(\d{5,6})',
            # Generic: standalone 5-6 digit number
            r'\b(\d{5,6})\b',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    async def _setup_code_handler(self, client: TelegramClient) -> None:
        """Устанавливает обработчик для перехвата кода от Telegram (user 777000)."""
        self._verification_code = None
        # Create Event in the running event loop to avoid loop mismatch
        self._code_event = asyncio.Event()
        self._code_event.clear()

        @client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_USER_ID))
        async def _on_login_code(event):
            code = self._extract_code_from_message(event.raw_text)
            if code:
                logger.info("Intercepted verification code (length=%d)", len(code))
                self._verification_code = code
                self._code_event.set()

        self._code_handler = _on_login_code

    async def _remove_code_handler(self, client: TelegramClient) -> None:
        """Удаляет обработчик кода."""
        if hasattr(self, '_code_handler'):
            client.remove_event_handler(self._code_handler)

    async def _wait_for_code(self, timeout: int = CODE_WAIT_TIMEOUT) -> Optional[str]:
        """
        Ждёт получения кода подтверждения через Telethon.

        Args:
            timeout: Максимальное время ожидания в секундах

        Returns:
            Код подтверждения или None если timeout
        """
        # Ensure event exists (creates in current event loop if needed)
        if self._code_event is None:
            self._code_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._code_event.wait(), timeout=timeout)
            return self._verification_code
        except asyncio.TimeoutError:
            logger.warning("Verification code timeout after %ds", timeout)
            return None

    async def _check_fragment_state(self, page) -> str:
        """
        Определяет текущее состояние на fragment.com.

        Returns:
            "authorized" - уже авторизован в Telegram на fragment.com
            "not_authorized" - не авторизован, нужно Connect Telegram
            "loading" - страница ещё загружается
            "unknown" - неизвестное состояние
        """
        try:
            # Check for "My Assets" or user menu - indicates Telegram is connected
            my_assets = await page.query_selector(
                'a[href*="my-assets"], a[href*="my_assets"], '
                '[class*="my-assets"], [class*="user-menu"], '
                '[class*="avatar"]'
            )
            if my_assets:
                return "authorized"

            # Check for "Connect Telegram" button - indicates not authorized
            connect_btn = await page.query_selector(
                'button:has-text("Connect Telegram"), '
                'a:has-text("Connect Telegram"), '
                '[class*="connect"]:has-text("Telegram")'
            )
            if connect_btn:
                return "not_authorized"

            # Check page text content
            body_text = await page.evaluate("() => document.body?.innerText || ''")
            if 'Connect TON and Telegram' in body_text:
                return "not_authorized"
            if 'My Assets' in body_text or 'My Numbers' in body_text:
                return "authorized"

            # Check if page has loaded at all
            title = await page.title()
            if 'Fragment' in title:
                return "not_authorized"  # Page loaded but no clear state

            return "loading"

        except Exception as e:
            logger.debug("Error checking fragment state: %s", e)
            return "unknown"

    async def _human_delay(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """Human-like random delay."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def _click_connect_telegram(self, page) -> bool:
        """
        Находит и нажимает кнопку "Connect Telegram" на fragment.com.

        Returns:
            True если кнопка найдена и нажата
        """
        selectors = [
            'button:has-text("Connect Telegram")',
            'a:has-text("Connect Telegram")',
            '[class*="connect"]:has-text("Telegram")',
            'text="Connect Telegram"',
            # Mobile menu
            'a:has-text("Connect")',
        ]

        for selector in selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await self._human_delay(0.3, 0.8)
                    await btn.click()
                    logger.info("Clicked 'Connect Telegram' button")
                    return True
            except Exception as e:
                logger.debug("Selector %s failed: %s", selector, e)
                continue

        # Fallback: try page.click with text matching
        try:
            await page.click('text="Connect Telegram"', timeout=5000)
            logger.info("Clicked 'Connect Telegram' via text match")
            return True
        except Exception:
            pass

        logger.warning("Could not find 'Connect Telegram' button")
        return False

    async def _enter_phone_number(self, page, phone: str) -> bool:
        """
        Вводит номер телефона в форму Telegram Login Widget.

        Args:
            phone: Номер телефона (e.g., "79991234567")

        Returns:
            True если номер введён и отправлен
        """
        # Wait for phone input to appear
        phone_selectors = [
            'input[type="tel"]',
            'input[placeholder*="phone"]',
            'input[placeholder*="Phone"]',
            'input[name="phone"]',
            '#phone',
            'input.phone-input',
        ]

        phone_input = None
        for selector in phone_selectors:
            try:
                phone_input = await page.wait_for_selector(
                    selector, timeout=10000, state="visible"
                )
                if phone_input:
                    break
            except Exception:
                continue

        if not phone_input:
            logger.warning("Phone input not found on page")
            return False

        # Format phone: ensure it starts with +
        formatted_phone = phone if phone.startswith('+') else f'+{phone}'
        logger.info("Entering phone: %s", _mask_phone(formatted_phone))

        # Clear and type phone number with human-like delay
        await phone_input.click()
        await self._human_delay(0.2, 0.5)

        # Clear existing content
        await phone_input.fill('')
        await self._human_delay(0.1, 0.3)

        # Type character by character for more human-like behavior
        for char in formatted_phone:
            await phone_input.type(char, delay=random.randint(50, 150))

        await self._human_delay(0.5, 1.0)

        # Submit: click Next/Continue button or press Enter
        submit_selectors = [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Send")',
            'button[type="submit"]',
            'input[type="submit"]',
        ]

        for selector in submit_selectors:
            try:
                submit_btn = await page.query_selector(selector)
                if submit_btn and await submit_btn.is_visible():
                    await submit_btn.click()
                    logger.info("Submitted phone number")
                    return True
            except Exception:
                continue

        # Fallback: press Enter
        await phone_input.press('Enter')
        logger.info("Submitted phone via Enter")
        return True

    async def _enter_verification_code(self, page, code: str) -> bool:
        """
        Вводит код подтверждения.

        Args:
            code: Код подтверждения (5-6 цифр)

        Returns:
            True если код введён
        """
        code_selectors = [
            'input[type="text"]',
            'input[type="number"]',
            'input[placeholder*="code"]',
            'input[placeholder*="Code"]',
            'input[name="code"]',
            '#code',
        ]

        code_input = None
        for selector in code_selectors:
            try:
                code_input = await page.wait_for_selector(
                    selector, timeout=10000, state="visible"
                )
                if code_input:
                    break
            except Exception:
                continue

        if not code_input:
            logger.warning("Code input not found on page")
            return False

        logger.info("Entering verification code")
        await code_input.click()
        await self._human_delay(0.2, 0.5)
        await code_input.fill('')

        # Type code character by character
        for char in code:
            await code_input.type(char, delay=random.randint(80, 200))

        await self._human_delay(0.5, 1.0)

        # Submit code
        submit_selectors = [
            'button:has-text("Sign In")',
            'button:has-text("Confirm")',
            'button:has-text("Submit")',
            'button:has-text("Next")',
            'button[type="submit"]',
        ]

        for selector in submit_selectors:
            try:
                submit_btn = await page.query_selector(selector)
                if submit_btn and await submit_btn.is_visible():
                    await submit_btn.click()
                    logger.info("Submitted verification code")
                    return True
            except Exception:
                continue

        # Fallback: press Enter
        await code_input.press('Enter')
        logger.info("Submitted code via Enter")
        return True

    async def _wait_for_auth_complete(
        self,
        page,
        timeout: int = AUTH_COMPLETE_TIMEOUT
    ) -> bool:
        """
        Ждёт завершения авторизации на fragment.com.

        Returns:
            True если авторизация успешна
        """
        for i in range(timeout):
            await asyncio.sleep(1)
            state = await self._check_fragment_state(page)
            if state == "authorized":
                logger.info("Fragment authorization complete!")
                return True
            if i % 5 == 0 and i > 0:
                logger.debug("Waiting for auth completion... (%ds)", i)

        logger.warning("Auth completion timeout after %ds", timeout)
        return False

    async def connect(
        self,
        headless: bool = False,
    ) -> FragmentResult:
        """
        Полный цикл авторизации на fragment.com.

        1. Открыть fragment.com в существующем browser profile
        2. Если уже авторизован — вернуть success
        3. Если нет — нажать Connect Telegram, ввести телефон, перехватить код
        4. Вернуть результат

        Args:
            headless: Headless режим браузера

        Returns:
            FragmentResult с результатом
        """
        profile = self.browser_manager.get_profile(
            self.account.name,
            self.account.proxy
        )

        logger.info("=" * 60)
        logger.info("FRAGMENT.COM AUTHORIZATION")
        logger.info("Account: %s", self.account.name)
        logger.info("Profile: %s", profile.name)
        logger.info("=" * 60)

        client = None
        browser_ctx = None

        try:
            # 1. Connect Telethon client
            logger.info("[1/5] Connecting Telethon client...")
            client = await self._create_telethon_client()
            self._client = client

            # Get phone number for login
            me = await client.get_me()
            phone = me.phone
            if not phone:
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Phone number not available from Telethon session"
                )

            # Setup code interception handler
            await self._setup_code_handler(client)

            # 2. Launch browser
            logger.info("[2/5] Launching browser...")
            browser_extra_args = {
                "os": self.account.device.browser_os_list,
            }
            browser_ctx = await self.browser_manager.launch(
                profile,
                headless=headless,
                extra_args=browser_extra_args
            )
            page = await browser_ctx.new_page()
            await page.set_viewport_size({"width": 1280, "height": 800})

            # 3. Open fragment.com
            logger.info("[3/5] Opening fragment.com...")
            try:
                await page.goto(FRAGMENT_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            except Exception as e:
                logger.warning("Page load warning: %s", e)

            # Wait for page to stabilize
            await asyncio.sleep(3)

            # Check current state
            state = await self._check_fragment_state(page)
            logger.info("Current fragment state: %s", state)

            if state == "authorized":
                logger.info("Already authorized on fragment.com!")
                return FragmentResult(
                    success=True,
                    account_name=self.account.name,
                    already_authorized=True,
                    telegram_connected=True
                )

            # 4. Connect Telegram
            logger.info("[4/5] Connecting Telegram on fragment.com...")

            # Click "Connect Telegram"
            if not await self._click_connect_telegram(page):
                # Try scrolling or looking in a menu
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)
                if not await self._click_connect_telegram(page):
                    return FragmentResult(
                        success=False,
                        account_name=self.account.name,
                        error="Could not find 'Connect Telegram' button"
                    )

            await self._human_delay(1.0, 2.0)

            # Enter phone number
            if not await self._enter_phone_number(page, phone):
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Could not enter phone number"
                )

            await self._human_delay(1.0, 2.0)

            # 5. Wait for and enter verification code
            logger.info("[5/5] Waiting for verification code...")

            code = await self._wait_for_code(timeout=CODE_WAIT_TIMEOUT)
            if not code:
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Verification code not received within timeout"
                )

            # Enter the code
            if not await self._enter_verification_code(page, code):
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Could not enter verification code"
                )

            # Wait for auth completion
            if await self._wait_for_auth_complete(page):
                return FragmentResult(
                    success=True,
                    account_name=self.account.name,
                    telegram_connected=True
                )

            return FragmentResult(
                success=False,
                account_name=self.account.name,
                error="Authorization did not complete within timeout"
            )

        except FloodWaitError as e:
            logger.error("FloodWait: %ds", e.seconds)
            return FragmentResult(
                success=False,
                account_name=self.account.name,
                error=f"FloodWait: {e.seconds}s"
            )
        except Exception as e:
            logger.error("Fragment auth error: %s", e, exc_info=True)
            return FragmentResult(
                success=False,
                account_name=self.account.name,
                error=str(e)
            )
        finally:
            # Cleanup code handler
            if client:
                try:
                    await self._remove_code_handler(client)
                except Exception:
                    pass

            # Close browser
            if browser_ctx:
                try:
                    await browser_ctx.close()
                except Exception as e:
                    logger.warning("Error closing browser: %s", e)

            # Disconnect Telethon
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
