# TODO: Fragment Auth — Fixes Before Live Test

> **Date:** 2026-02-04
> **Source:** Self-review (code review + manual analysis)
> **Priority:** Fix BEFORE live testing on real accounts

---

## CRITICAL (fix first)

### 1. asyncio.Event race condition
**File:** `src/fragment_auth.py:80`
**Problem:** `_code_event = asyncio.Event()` created in `__init__`, may be in a different event loop than `connect()`. Breaks in GUI apps.
**Fix:** Create `_code_event` inside `connect()` or `_setup_code_handler()`.

### 2. Overly broad regex catches wrong numbers
**File:** `src/fragment_auth.py:145`
**Problem:** Last pattern `r'\b(\d{5,6})\b'` matches ANY 5-6 digit number. IP addresses, timestamps from user 777000 messages will be caught as "codes".
**Fix:** Remove generic fallback or add keyword guard (message must contain "login"/"code"/"sign in").

### 3. SQLite connection leak
**File:** `src/fragment_auth.py:94-99`
**Problem:** `conn.close()` not in `try/finally`. If PRAGMA fails — connection leaks, database locked.
**Fix:** `with sqlite3.connect(...) as conn:`

### 4. Phone number leak in exception logs
**File:** `src/fragment_auth.py:604-610`
**Problem:** `str(e)` may contain phone number. Violates own rule "НЕ логировать phone numbers".
**Fix:** Sanitize exception message with `_mask_phone()` before logging/storing.

---

## IMPORTANT (fix before scaling)

### 5. CSS selectors are ALL GUESSED
**File:** `src/fragment_auth.py:202-216, 249-256, 292-299, 368-375, 404-410`
**Problem:** None of the selectors were verified on real fragment.com. ~50% will likely fail.
**Action:** First live test — open fragment.com in DevTools, record real selectors, update code.

### 6. Telegram Login Widget may be popup/iframe
**Problem:** Code assumes login form is on fragment.com page. If Widget opens as popup window — all `page.query_selector()` calls fail.
**Action:** Check during live test. If popup — need `page.wait_for_event('popup')` or iframe handling.

### 7. Missing type hints on `page` parameter
**Files:** 5 methods in fragment_auth.py
**Fix:** Add `from playwright.async_api import Page` under `TYPE_CHECKING`, annotate `page: Page`.

### 8. Silent exception swallowing in cleanup
**File:** `src/fragment_auth.py:614-617, 629-631`
**Fix:** Add `logger.debug("Cleanup error: %s", e)` instead of bare `pass`.

### 9. No phone number validation
**File:** `src/fragment_auth.py:281`
**Fix:** Validate `re.match(r'^\+?\d{7,15}$', phone)` before entering into browser.

### 10. No retry logic
**Problem:** Any failure on any step = immediate failure. telegram_auth has exponential backoff.
**Fix:** Add retry wrapper for `_click_connect_telegram`, `_enter_phone_number`, `_enter_verification_code`.

### 11. Test coverage gaps
**Missing tests for:**
- `connect()` full happy path (phone → code → auth)
- `connect()` when phone is None
- `connect()` when Connect Telegram button not found
- `connect()` FloodWaitError path
- `_click_connect_telegram()` with mocked page
- `_enter_phone_number()` with mocked page
- `_enter_verification_code()` with mocked page
- `_setup_code_handler()` / `_remove_code_handler()`

### 12. _mask_phone edge case
**File:** `src/fragment_auth.py:44-48`
**Problem:** 7-8 char phones reveal most digits. `+123456` → `+123***3456` (5 of 7 visible).
**Fix:** Mask proportionally or ensure ≥3 chars always hidden.

---

## NICE TO HAVE

- Extract CSS selectors into class-level constants (reduce duplication)
- Use `page.wait_for_selector()` in `_wait_for_auth_complete` instead of polling
- Add `_code_event` freshness check to prevent stale codes from previous runs
- Configurable timeouts via constructor params

---

## Live Test Checklist (first run on real account)

```
[ ] Open fragment.com manually in Camoufox — does it load?
[ ] Inspect DOM — record actual CSS selectors for:
    [ ] "Connect Telegram" button
    [ ] Phone input field
    [ ] Code input field
    [ ] "Next"/"Submit" buttons
    [ ] "My Assets" / authorized state indicator
[ ] Is Login Widget inline, iframe, or popup?
[ ] Run fragment auth on Софт 313 — observe browser
[ ] Check if code arrives from user 777000
[ ] Check if code extraction regex works
[ ] Verify session persists after browser close
[ ] Check Telethon session still works after Fragment auth
```
