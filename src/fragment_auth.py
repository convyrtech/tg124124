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
from pathlib import Path
from typing import Any, Optional

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
            auto_reconnect=False,
            connection_retries=0,
            # FIX-3.1: MUST be True — event handler for 777000 codes needs update dispatch
            receive_updates=True,
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
            r'code\s+is\s+(\d{5,6})',
            # Fallback: standalone code on its own line (not embedded in unrelated numbers)
            r'^\s*(\d{5,6})\s*$',
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
            # Primary: check Aj.state.unAuth JS variable (minified, may break on update)
            is_unauth = await page.evaluate(
                "() => (typeof Aj !== 'undefined' && Aj.state) ? Aj.state.unAuth : null"
            )
            if is_unauth is False:
                return "authorized"
            if is_unauth is True:
                return "not_authorized"

            # FIX-3.2: Fallback selectors — by CSS class first, then by text content
            has_login_btn = await page.evaluate(
                "() => !!document.querySelector('button.login-link')"
            )
            # Text-based fallback for login button (robust against CSS class changes)
            if not has_login_btn:
                has_login_btn = await page.evaluate("""
                    () => !!Array.from(document.querySelectorAll('button'))
                        .find(b => /log\\s*in|connect\\s*telegram/i.test(b.textContent))
                """)
            if has_login_btn:
                return "not_authorized"

            # Fallback: check for logout link (means logged in)
            has_logout = await page.evaluate(
                "() => !!document.querySelector('.logout-link')"
            )
            # Text-based fallback for logout indicator
            if not has_logout:
                has_logout = await page.evaluate("""
                    () => !!Array.from(document.querySelectorAll('a, button'))
                        .find(el => /log\\s*out|disconnect/i.test(el.textContent))
                """)
            if has_logout:
                return "authorized"

            # Fallback: check stel_ssid cookie (works even when JS doesn't init)
            try:
                cookies = await page.context.cookies(["https://fragment.com"])
                if any(c["name"] == "stel_ssid" for c in cookies):
                    return "authorized"
            except Exception:
                pass

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
        # Wait for page JS to fully initialize before clicking
        await asyncio.sleep(2)
        popup_promise = page.wait_for_event('popup', timeout=20000)
        # FIX-3.2: Try CSS class first, then text-based fallback
        try:
            await page.click('button.login-link')
        except Exception:
            # Fallback: click by text content
            login_btn = await page.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => /log\\s*in|connect\\s*telegram/i.test(b.textContent));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """)
            if not login_btn:
                raise RuntimeError("Login button not found on fragment.com")
        popup = await popup_promise
        await popup.wait_for_load_state('domcontentloaded')
        logger.info("OAuth popup opened: %s", popup.url)
        return popup

    async def _check_popup_already_logged_in(self, popup: Any) -> bool:
        """
        Checks if oauth.telegram.org popup shows 'already logged in' form.

        When the user has an existing stel_ssid cookie on oauth.telegram.org,
        the popup shows ACCEPT/DECLINE buttons instead of the phone input form.

        Args:
            popup: Playwright page (oauth.telegram.org popup)

        Returns:
            True if already-logged-in form is shown (no phone input needed)
        """
        has_phone = await popup.evaluate(
            "() => !!document.getElementById('login-phone')"
        )
        if has_phone:
            return False

        # Check for ACCEPT button (already logged in flow)
        body_text = await popup.evaluate(
            "() => document.body.innerText"
        )
        if 'ACCEPT' in body_text and 'DECLINE' in body_text:
            logger.info("Popup shows already-logged-in form (ACCEPT/DECLINE)")
            return True

        return False

    async def _accept_existing_session(self, popup: Any) -> bool:
        """
        Clicks ACCEPT on the already-logged-in oauth popup.

        When the user has a valid stel_ssid cookie, oauth.telegram.org
        shows a permission confirmation instead of the phone input form.
        Clicking ACCEPT grants Fragment access without Telethon confirmation.

        Args:
            popup: Playwright page (oauth.telegram.org popup)

        Returns:
            True if ACCEPT was clicked successfully
        """
        try:
            # Wait for popup content to load
            await popup.wait_for_load_state("domcontentloaded", timeout=5000)
            await asyncio.sleep(1)

            # Primary: find ACCEPT via JS and click it directly.
            # oauth.telegram.org uses <a> elements with various classes —
            # Playwright's get_by_text can miss them due to text casing/structure.
            clicked = await popup.evaluate("""() => {
                // Strategy 1: find all clickable elements containing target text
                const targets = ['ACCEPT', 'Accept', 'Log In', 'Confirm'];
                const elements = document.querySelectorAll('a, button, div[role="button"], span[role="button"]');
                for (const el of elements) {
                    const text = (el.textContent || '').trim();
                    for (const target of targets) {
                        if (text === target) {
                            el.click();
                            return 'clicked:' + target;
                        }
                    }
                }
                // Strategy 2: look for submit button in any form
                const submit = document.querySelector('button[type="submit"], input[type="submit"]');
                if (submit) {
                    submit.click();
                    return 'clicked:submit';
                }
                return null;
            }""")

            if clicked:
                logger.info("Clicked accept via JS evaluate: %s", clicked)
                return True

            # Fallback: Playwright locator-based search
            for text in ("ACCEPT", "Accept", "Log In", "LOG IN", "Confirm"):
                try:
                    btn = popup.get_by_text(text, exact=True)
                    if await btn.count() > 0:
                        await btn.first.click(timeout=5000)
                        logger.info("Clicked '%s' via get_by_text", text)
                        return True
                except Exception:
                    continue

            # Debug: log popup HTML for investigation
            try:
                html = await popup.evaluate("document.body.innerHTML")
                logger.warning("Could not find ACCEPT button. HTML: %s", html[:500])
            except Exception:
                pass
            return False
        except Exception as e:
            logger.error("Failed to click ACCEPT: %s", e)
            return False

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

        # FIX-3.2: Click submit with fallback
        try:
            await popup.click('form#send-form button[type="submit"]')
        except Exception:
            await popup.evaluate("""
                () => {
                    const btn = document.querySelector('button[type="submit"]')
                        || Array.from(document.querySelectorAll('button'))
                            .find(b => /next|submit|send/i.test(b.textContent));
                    if (btn) btn.click();
                }
            """)

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

            # Variant C: unknown message format — do NOT auto-confirm,
            # it may be an unrelated stale message from 777000
            logger.warning("Unknown message from 777000 (not confirming): %s", event.raw_text[:100])

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

        # Fallback: check if stel_ssid cookie exists on fragment.com
        # JS state (Aj.state.unAuth) may not initialize properly in headless mode,
        # but cookies are a reliable indicator of server-side auth success.
        try:
            cookies = await page.context.cookies(["https://fragment.com"])
            has_ssid = any(c["name"] == "stel_ssid" for c in cookies)
            if has_ssid:
                logger.info("Fragment auth detected via stel_ssid cookie (JS state unavailable)")
                return True
        except Exception as e:
            logger.debug("Cookie check failed: %s", e)

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

            # Check if popup shows already-logged-in form (ACCEPT/DECLINE)
            if await self._check_popup_already_logged_in(popup):
                logger.info("[5/6] Already logged in on oauth — clicking ACCEPT...")
                if not await self._accept_existing_session(popup):
                    return FragmentResult(
                        success=False,
                        account_name=self.account.name,
                        error="Failed to click ACCEPT on existing session popup"
                    )
                logger.info("[6/6] Skipping Telethon confirmation (existing session)")
            else:
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
            # After OAuth confirmation, popup may close and redirect.
            # Give it a moment then reload to pick up the new auth state.
            await self._human_delay(2.0, 3.0)
            try:
                await page.reload(timeout=15000)
            except Exception:
                pass  # Page might have already navigated
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


async def fragment_account(
    account_dir: Path,
    password_2fa: Optional[str] = None,
    headless: bool = True,
    proxy_override: Optional[str] = None,
    browser_manager: Optional[BrowserManager] = None,
) -> AuthResult:
    """Authorize one account on fragment.com. Pool-compatible wrapper.

    Args:
        account_dir: Directory with session, api.json, ___config.json.
        password_2fa: Unused (kept for API compat with migrate_account).
        headless: Run browser headless.
        proxy_override: Proxy string from DB.
        browser_manager: Shared BrowserManager instance.

    Returns:
        AuthResult for compatibility with MigrationWorkerPool.
    """
    account = AccountConfig.load(Path(account_dir))
    if proxy_override is not None:
        account.proxy = proxy_override
    auth = FragmentAuth(account, browser_manager or BrowserManager())
    result = await auth.connect(headless=headless)

    return AuthResult(
        success=result.success,
        profile_name=account.name,
        error=result.error,
        user_info={"already_authorized": result.already_authorized} if result.success else None,
    )
