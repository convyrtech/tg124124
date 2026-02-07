"""
Fragment.com Authorization Module
Авторизация на fragment.com через OAuth popup на oauth.telegram.org + Telethon подтверждение.

Flow:
1. Открываем fragment.com в Camoufox browser profile
2. Проверяем: уже авторизован? (через Aj.state.unAuth JS)
3. Если нет — кликаем "Connect Telegram" (button.login-link)
4. Ловим popup на oauth.telegram.org
5. Вводим номер телефона в popup форму
6. Telethon перехватывает сообщение от user 777000 и подтверждает (кнопка/код)
7. Popup закрывается, fragment.com авторизован

ВАЖНО: НЕ логировать auth_key, api_hash, phone numbers полностью!
"""
import asyncio
import logging
import random
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from .browser_manager import BrowserManager, BrowserContext
from .telegram_auth import AccountConfig, parse_telethon_proxy

logger = logging.getLogger(__name__)


FRAGMENT_URL = "https://fragment.com"
# Telegram service account that sends login codes
TELEGRAM_SERVICE_USER_ID = 777000

# Timeouts
PAGE_LOAD_TIMEOUT = 60000  # ms
CONFIRM_TIMEOUT = 60  # seconds - wait for Telethon confirmation
AUTH_COMPLETE_TIMEOUT = 30  # seconds - wait for fragment.com state change


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
    Авторизация на fragment.com через OAuth popup + Telethon.

    Использует:
    - Существующий Camoufox browser profile (тот же что для web.telegram.org)
    - Telethon client для подтверждения login request
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

    async def _create_telethon_client(self) -> TelegramClient:
        """
        Создаёт Telethon client из существующей сессии.

        Returns:
            Connected and authorized TelegramClient

        Raises:
            RuntimeError: If connection or authorization fails
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

        Args:
            text: Raw text of the message

        Returns:
            Extracted code string or None
        """
        if not text:
            return None

        patterns = [
            r'Login code:\s*(\d{5,6})',
            r'Код входа:\s*(\d{5,6})',
            r'login code[:\s]+(\d{5,6})',
            r'code[:\s]+(\d{5,6})',
            r'\b(\d{5,6})\b',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    async def _human_delay(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """Human-like random delay."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def _check_fragment_state(self, page: Any) -> str:
        """
        Определяет состояние авторизации на fragment.com.

        Uses real JS API (Aj.state.unAuth) and DOM selectors confirmed via Playwright MCP.

        Args:
            page: Playwright page object

        Returns:
            "authorized", "not_authorized", "loading", or "unknown"
        """
        try:
            # Primary: check Aj.state.unAuth JS variable
            is_unauth = await page.evaluate(
                "() => (typeof Aj !== 'undefined' && Aj.state) ? Aj.state.unAuth : null"
            )
            if is_unauth is False:
                return "authorized"
            if is_unauth is True:
                return "not_authorized"

            # Fallback: check for Connect Telegram button
            has_login_btn = await page.evaluate(
                "() => !!document.querySelector('button.login-link')"
            )
            if has_login_btn:
                return "not_authorized"

            # Fallback: check for logout link (means logged in)
            has_logout = await page.evaluate(
                "() => !!document.querySelector('.logout-link')"
            )
            if has_logout:
                return "authorized"

            return "loading"
        except Exception as e:
            logger.debug("Error checking fragment state: %s", e)
            return "unknown"

    async def _open_oauth_popup(self, page: Any) -> Any:
        """
        Кликает Connect Telegram и перехватывает popup окно oauth.telegram.org.

        Args:
            page: Playwright page on fragment.com

        Returns:
            Popup page object (oauth.telegram.org)

        Raises:
            RuntimeError: If popup doesn't appear or button not found
        """
        popup_promise = page.wait_for_event('popup', timeout=10000)
        await page.click('button.login-link')
        popup = await popup_promise
        await popup.wait_for_load_state('domcontentloaded')
        logger.info("OAuth popup opened: %s", popup.url)
        return popup

    async def _submit_phone_on_popup(self, popup: Any, phone: str) -> bool:
        """
        Заполняет телефон на oauth.telegram.org popup и сабмитит.

        Args:
            popup: Playwright page (oauth.telegram.org popup)
            phone: Phone number without + (e.g. "79991234567")

        Returns:
            True if phone was accepted and confirmation form appeared
        """
        if not phone or not phone.strip():
            logger.error("Cannot submit empty phone number")
            return False

        formatted = f'+{phone}' if not phone.startswith('+') else phone
        logger.info("Submitting phone: %s", _mask_phone(formatted))

        # Set phone value via JS (bypasses country code selection complexity)
        await popup.evaluate("""(phone) => {
            const codeEl = document.getElementById('login-phone-code');
            const phoneEl = document.getElementById('login-phone');
            if (codeEl) codeEl.value = '';
            if (phoneEl) phoneEl.value = phone;
        }""", formatted)

        # Click submit
        await popup.click('form#send-form button[type="submit"]')

        # Wait for confirmation form to appear (#login-form loses .hide class)
        try:
            await popup.wait_for_selector('#login-form:not(.hide)', timeout=15000)
            logger.info("Phone accepted, confirmation form visible")
            return True
        except Exception as e:
            logger.debug("Phone submission wait failed: %s", e)
            # Check for error message
            error = await popup.evaluate(
                "() => document.getElementById('login-alert')?.textContent || ''"
            )
            if error:
                logger.error("OAuth phone error: %s", error)
            return False

    async def _confirm_via_telethon(self, client: TelegramClient, timeout: int = CONFIRM_TIMEOUT) -> bool:
        """
        Перехватывает и подтверждает login request через Telethon.

        Handles multiple confirmation types:
        - Inline button (KeyboardButtonCallback): clicks Confirm/Accept
        - Text code: extracts and logs (popup auto-confirms on read in some cases)
        - URL auth button: logs for investigation

        Args:
            client: Connected TelegramClient
            timeout: Max seconds to wait for confirmation message

        Returns:
            True if confirmation was handled
        """
        confirmed = asyncio.Event()

        @client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_USER_ID))
        async def handler(event):
            msg = event.message

            # Variant A: inline button (Confirm / Accept / Подтвердить)
            if msg.buttons:
                for row_idx, row in enumerate(msg.buttons):
                    for col_idx, btn in enumerate(row):
                        btn_text = btn.text.lower()
                        if any(kw in btn_text for kw in ('confirm', 'accept', 'подтвердить', 'принять')):
                            logger.info("Clicking confirmation button: %r", btn.text)
                            try:
                                await msg.click(row_idx, col_idx)
                            except Exception as e:
                                logger.error("Failed to click confirmation button: %s", e)
                            confirmed.set()
                            return
                # If buttons exist but none matched keywords, click first button
                logger.warning("Unknown button layout, clicking first button")
                try:
                    await msg.click(0, 0)
                except Exception as e:
                    logger.error("Failed to click fallback button: %s", e)
                confirmed.set()
                return

            # Variant B: text code (fallback)
            code = self._extract_code_from_message(event.raw_text)
            if code:
                logger.info("Received text code (length=%d) — auto-confirm may apply", len(code))
                confirmed.set()
                return

            # Variant C: unknown message format
            logger.warning("Unknown message from 777000: %s", event.raw_text[:100])
            confirmed.set()

        try:
            await asyncio.wait_for(confirmed.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Confirmation timeout after %ds", timeout)
            return False
        finally:
            client.remove_event_handler(handler)

    async def _wait_for_fragment_auth(self, page: Any, timeout: int = AUTH_COMPLETE_TIMEOUT) -> bool:
        """
        Ждёт пока fragment.com покажет авторизованное состояние.

        After Telethon confirms, the oauth popup polls /auth/login and eventually
        redirects fragment.com to authorized state.

        Args:
            page: Playwright page on fragment.com
            timeout: Max seconds to wait

        Returns:
            True if authorized state detected
        """
        for i in range(timeout):
            await asyncio.sleep(1)
            try:
                state = await self._check_fragment_state(page)
                if state == "authorized":
                    logger.info("Fragment authorization complete!")
                    return True
            except Exception as e:
                logger.debug("State check error (page may be reloading): %s", e)
            if i % 5 == 0 and i > 0:
                logger.debug("Waiting for fragment auth... (%ds)", i)

        logger.warning("Fragment auth timeout after %ds", timeout)
        return False

    async def connect(
        self,
        headless: bool = False,
    ) -> FragmentResult:
        """
        Полный цикл авторизации на fragment.com.

        1. Telethon connect + get phone
        2. Browser launch + open fragment.com
        3. Check if already authorized
        4. Open oauth popup + submit phone
        5. Confirm via Telethon
        6. Wait for fragment.com authorized state

        Args:
            headless: Run browser in headless mode

        Returns:
            FragmentResult with outcome
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
            logger.info("[1/6] Connecting Telethon client...")
            client = await self._create_telethon_client()
            self._client = client

            me = await client.get_me()
            phone = me.phone
            if not phone:
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Phone number not available from Telethon session"
                )

            # 2. Launch browser
            logger.info("[2/6] Launching browser...")
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
            logger.info("[3/6] Opening fragment.com...")
            try:
                await page.goto(FRAGMENT_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            except Exception as e:
                logger.warning("Page load warning: %s", e)

            # Wait for page JS to initialize
            state = "loading"
            for _ in range(10):
                await asyncio.sleep(0.5)
                state = await self._check_fragment_state(page)
                if state != "loading":
                    break
            logger.info("Current fragment state: %s", state)

            if state == "authorized":
                logger.info("Already authorized on fragment.com!")
                return FragmentResult(
                    success=True,
                    account_name=self.account.name,
                    already_authorized=True,
                    telegram_connected=True
                )

            # 5. Open OAuth popup
            logger.info("[4/6] Opening OAuth popup...")
            try:
                popup = await self._open_oauth_popup(page)
            except Exception as e:
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error=f"Failed to open OAuth popup: {e}"
                )

            await self._human_delay(0.5, 1.0)

            # 6. Submit phone on popup
            logger.info("[5/6] Submitting phone on OAuth popup...")
            if not await self._submit_phone_on_popup(popup, phone):
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Phone submission failed on OAuth popup"
                )

            # 7. Confirm via Telethon (Telethon listens, popup polls /auth/login)
            logger.info("[6/6] Waiting for Telethon confirmation...")
            if not await self._confirm_via_telethon(client, timeout=CONFIRM_TIMEOUT):
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Confirmation timeout — no message from Telegram"
                )

            # 8. Wait for fragment.com to show authorized
            if not await self._wait_for_fragment_auth(page, timeout=AUTH_COMPLETE_TIMEOUT):
                return FragmentResult(
                    success=False,
                    account_name=self.account.name,
                    error="Fragment auth did not complete after confirmation"
                )

            return FragmentResult(
                success=True,
                account_name=self.account.name,
                telegram_connected=True
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
                except Exception as e:
                    logger.debug("Error disconnecting Telethon: %s", e)
