# TG Web Auth Migration Tool

## MANDATORY: Read Before ANY Work
**ALWAYS read `.claude/MASTER_PROMPT.md` at the start of EVERY session or after context loss.**
It contains: project context, all available tools, work methodology, safety rules, progress tracker, and context recovery protocol.

**Key documents:**
- `.claude/MASTER_PROMPT.md` - Master operating guide (READ FIRST)
- `docs/plans/2026-02-09-production-1000-design.md` - **CURRENT** production plan (Phase 1 done, Phase 2 next)
- `docs/plans/2026-02-10-full-audit-plan.md` - Production audit findings (85% ready)
- `docs/plans/2026-02-11-exe-packaging-plan.md` - PyInstaller EXE packaging plan

## Project Goal
Автоматическая миграция Telegram session файлов (Telethon) в браузерные профили для:
- **web.telegram.org** - основной веб-клиент (QR Login)
- **fragment.com** - NFT usernames, Telegram Stars (OAuth popup)

Масштаб: **1000 аккаунтов**, переносимость между ПК.

## Current Status (2026-02-12)

### Что работает
- Programmatic QR Login (без ручного сканирования)
- Multi-decoder QR (zxing-cpp primary, OpenCV, pyzbar fallbacks)
- Camoufox antidetect browser с persistent profiles
- SOCKS5 proxy с auth через pproxy relay
- Batch миграция (sequential + parallel через ParallelMigrationController)
- Fragment.com OAuth popup flow (live verified 1/1, commit 3388c58)
- SQLite metadata (accounts, proxies, migrations, batches, operation_log) + WAL + busy_timeout=30s
- Worker pool (asyncio queue, retry, circuit breaker, mode web/fragment) - интегрирован в GUI
- Shared BrowserManager в worker pool (LRU eviction работает глобально)
- Profile lifecycle (hot/cold tiering, LRU eviction, zip compression)
- Proxy management (import, health check, auto-replace dead)
- Auth TTL 365 days (SetAuthorizationTTLRequest после миграции)
- Resource monitor (CPU/RAM, adaptive concurrency)
- PID-based force-kill zombie browsers (psutil, не taskkill /IM)
- QR decode: zxing-cpp + morphological preprocessing (100% rate на dot-style и thin-line QR)
- BrowserWatchdog: thread-based kill при зависании page.goto() на Windows (240s timeout)
- Pre-check: skip browser launch for already-migrated profiles (storage_state.json user_auth check)
- GUI (DearPyGui, 90% complete): Migrate All, Retry Failed, Fragment All, STOP, progress throttle, fragment_status column
- CLI: 9 команд (migrate, open, list, check, health, fragment, check-proxies, proxy-refresh, init)
- CLI atexit: psutil orphan killer (pproxy via cmdline, camoufox/firefox via name)
- Proxy relay recreation on browser launch retry (fresh port, no broken state)
- Circuit breaker single-probe half-open (prevents worker flood after reset)
- Global batch pause (asyncio.Event pauses ALL workers, not just one)
- Account dedup in worker pool (prevents AUTH_KEY_DUPLICATED)
- QR token: non-retryable errors (EXPIRED/INVALID) fail immediately
- GUI: per-row buttons disabled during batch, log deque(500), incremental table update
- Async zip I/O in ProfileLifecycleManager (run_in_executor)
- Proxy credentials stripped from profile_config.json and error messages
- assign_proxy() checks for already-assigned proxy
- PyInstaller EXE packaging (one-folder dist, frozen exe support)
- 326 тестов проходят

### Pre-production Audit (2026-02-12, commit ee5957b)
6 критических багов найдены и исправлены:
1. **FloodWait detection** — `"FLOOD_WAIT" in "FloodWait: 30s"` = False → case-insensitive match
2. **CancelledError leak** — `except Exception` не ловит CancelledError (Python 3.11+) → `except BaseException`
3. **Batch pause deadlock** — workers на `batch_pause_event.wait()` не потребляли stop sentinels → `event.set()` before sentinels
4. **migrate_accounts_parallel dedup** — дубли → AUTH_KEY_DUPLICATED → `dict.fromkeys()`
5. **ParallelMigrationController cooldown=5s** — 7200 логинов/час → `max(cooldown, MIN_COOLDOWN)`
6. **GUI shutdown mid-flight** — `loop.stop()` без ожидания workers → poll `_active_pool` up to 30s

### Что НЕ работает / НЕ доделано
- **FIX-005** - 2FA selector hardcoded (P2)
- **psutil.cpu_percent** - первый вызов возвращает 0.0 (P3, cosmetic)
- **find_free_port TOCTOU** — порт может быть занят между bind и pproxy startup (P3, retry handles it)

## Architecture

### Programmatic QR Login Flow
```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Telethon  │     │   Camoufox   │     │  Telegram Web   │
│   Client    │     │   Browser    │     │    Server       │
└──────┬──────┘     └──────┬───────┘     └────────┬────────┘
       │                   │                      │
       │  1. Connect with existing session        │
       │─────────────────────────────────────────>│
       │                   │                      │
       │                   │  2. Open web.telegram.org
       │                   │─────────────────────>│
       │                   │                      │
       │                   │  3. Show QR code     │
       │                   │<─────────────────────│
       │                   │                      │
       │  4. Screenshot QR │                      │
       │<──────────────────│                      │
       │                   │                      │
       │  5. Decode token from QR                 │
       │  6. AcceptLoginTokenRequest(token)       │
       │─────────────────────────────────────────>│
       │                   │                      │
       │                   │  7. Session authorized
       │                   │<─────────────────────│
       │                   │                      │
       │  8. SetAuthorizationTTL(365 days)        │
       │─────────────────────────────────────────>│
       │                   │                      │
       │  9. Save browser profile                 │
       │<──────────────────│                      │
```

### Fragment.com OAuth Flow
```
Camoufox → fragment.com → Click "Log in"
  → popup: oauth.telegram.org
    → Already logged in? → Click "ACCEPT" → done
    → Not logged in? → Enter phone → Telethon listens for code from 777000
      → Enter code → Authorized → popup closes
  → fragment.com gets stel_ssid cookie → done
```

### Proxy Relay (для SOCKS5 auth)
```
Browser ──HTTP──> pproxy (localhost:random) ──SOCKS5+auth──> Remote Proxy
```

## File Structure (11156 строк src/, 326 тестов)
```
tg-web-auth/
├── accounts/                # Исходные session файлы (.gitignore)
│   └── account_name/
│       ├── session.session
│       ├── api.json
│       └── ___config.json
├── profiles/                # Browser profiles (.gitignore)
├── data/                    # SQLite database (.gitignore)
│   └── tgwebauth.db
├── src/                     # 11156 строк
│   ├── telegram_auth.py     # QR auth + AcceptLoginToken (2454 строк)
│   ├── fragment_auth.py     # Fragment.com OAuth popup + fragment_account() (742 строк)
│   ├── browser_manager.py   # Camoufox + ProfileLifecycleManager + PID kill (849 строк)
│   ├── worker_pool.py       # Asyncio queue pool, mode web/fragment (746 строк)
│   ├── cli.py               # CLI 9 команд (1291 строк)
│   ├── database.py          # SQLite: accounts, proxies, migrations, WAL (1073 строк)
│   ├── proxy_manager.py     # Import, health check, auto-replace (438 строк)
│   ├── proxy_relay.py       # SOCKS5→HTTP relay via pproxy (329 строк)
│   ├── proxy_health.py      # Batch TCP check (244 строк)
│   ├── resource_monitor.py  # CPU/RAM monitoring (162 строк)
│   ├── security_check.py    # Fingerprint/WebRTC check (380 строк)
│   ├── paths.py             # Centralized path resolution (dev + frozen exe)
│   ├── exception_handler.py # Global crash hook (sys.excepthook + asyncio)
│   ├── utils.py             # Proxy parsing helpers (103 строк)
│   ├── logger.py            # Logging setup + RotatingFileHandler (115 строк)
│   ├── pproxy_wrapper.py    # pproxy process (dev mode only, 23 строк)
│   └── gui/
│       ├── app.py           # DearPyGui main window + diagnostics (1720 строк)
│       ├── controllers.py   # GUI business logic (266 строк)
│       └── theme.py         # Hacker dark green theme (99 строк)
├── tests/                   # 326 тестов
│   ├── test_telegram_auth.py
│   ├── test_fragment_auth.py
│   ├── test_browser_manager.py
│   ├── test_proxy_manager.py
│   ├── test_proxy_health.py
│   ├── test_proxy_relay.py
│   ├── test_worker_pool.py
│   ├── test_resource_monitor.py
│   ├── test_database.py
│   ├── test_integration.py
│   ├── test_utils.py
│   └── conftest.py
├── docs/                    # Документация (3 плана)
│   └── plans/
│       ├── 2026-02-09-production-1000-design.md
│       ├── 2026-02-10-full-audit-plan.md
│       └── 2026-02-11-exe-packaging-plan.md
├── TGWebAuth.spec           # PyInstaller spec (one-folder dist)
├── build_exe.py             # Build script: PyInstaller + Camoufox copy + ZIP
├── main.py                  # Entry point for PyInstaller EXE
├── requirements.txt
├── CLAUDE.md
└── .gitignore
```

## Technical Stack
- **Python 3.11+** (async/await, asyncio)
- **Telethon** - MTProto client для AcceptLoginToken
- **Camoufox** - Antidetect browser (Firefox-based)
- **Playwright** - Browser automation
- **pproxy** - SOCKS5 auth relay
- **zxing-cpp/OpenCV/pyzbar** - QR decoding (zxing-cpp primary, 2 fallbacks)
- **Click** - CLI
- **aiosqlite** - Async SQLite
- **DearPyGui** - GUI
- **psutil** - Resource monitoring

## Commands

### Установка
```bash
pip install -r requirements.txt
python -m camoufox fetch     # скачать Camoufox browser
```

### Миграция
```bash
python -m src.cli migrate --account "Name"             # Один аккаунт
python -m src.cli migrate --all                        # Все аккаунты
python -m src.cli migrate --all --parallel 5           # Параллельно
python -m src.cli migrate --all --auto-scale           # Авто-параллельность
python -m src.cli migrate --resume                     # Продолжить прерванную
python -m src.cli migrate --retry-failed               # Повторить упавшие
python -m src.cli migrate --status                     # Статус batch
python -m src.cli migrate --account "Name" -p "2fa"    # С 2FA паролем
```

### Fragment
```bash
python -m src.cli fragment --account "Name"            # Один аккаунт
python -m src.cli fragment --all                       # Все аккаунты
python -m src.cli fragment --all --headed              # С GUI браузером
```

### Прокси
```bash
python -m src.cli check-proxies                        # Проверить все прокси в БД
python -m src.cli proxy-refresh -f proxies.txt         # Заменить мёртвые прокси
python -m src.cli proxy-refresh -f proxies.txt --auto  # Без подтверждения
python -m src.cli proxy-refresh -f proxies.txt --check-only  # Только проверить
```

### Другое
```bash
python -m src.cli open --account "Name"                # Открыть профиль в браузере
python -m src.cli list                                 # Список аккаунтов/профилей
python -m src.cli health --account "Name"              # Проверка здоровья аккаунта
python -m src.cli check -p "socks5:h:p:u:p"           # Fingerprint/WebRTC check
python -m src.cli init                                 # Инициализация директорий
```

### GUI
```bash
python -m src.gui.app                                  # Запуск GUI
```

### Тесты
```bash
pytest                    # Все 326 тестов
pytest -v                 # Verbose
pytest tests/test_proxy_manager.py -v  # Конкретный файл
```

## Database Schema (SQLite WAL)

```sql
accounts    (id, name, phone, username, session_path, proxy_id, status,
             last_check, error_message, created_at,
             fragment_status, web_last_verified, auth_ttl_days)

proxies     (id, host, port, username, password, protocol, status,
             assigned_account_id, last_check, created_at)
             UNIQUE(host, port)

migrations  (id, account_id, started_at, completed_at, success,
             error_message, profile_path, batch_id)

batches     (id, batch_id, total_count, started_at, finished_at)

operation_log (id, account_id, operation, success, error_message,
               details, created_at)
```

## Security Constraints

### ОБЯЗАТЕЛЬНО
- НЕ логировать auth_key, api_hash, passwords, tokens, phone numbers
- Изолированные browser profiles для каждого аккаунта
- 1 выделенный прокси на аккаунт (НИКОГДА не шарить)
- Graceful shutdown - корректно закрывать все ресурсы
- Cooldown 60-120s между аккаунтами (anti-ban)

### ЗАПРЕЩЕНО
- Hardcoded credentials
- `print()` вместо `logging`
- Bare `except:` (используй `except Exception as e:` или `except BaseException:` для cleanup)
- Игнорирование возвращаемых ошибок
- Использование одной session из двух клиентов одновременно

## GUI Testing Rules (ОБЯЗАТЕЛЬНО)

### После каждой GUI фичи:
1. [ ] Запустить приложение: `python -m src.gui.app`
2. [ ] Протестировать КАЖДУЮ кнопку вручную
3. [ ] Проверить что нет крашей
4. [ ] Все ошибки логируются, не молча падают

### Запрещено:
- Говорить "готово" без ручного тестирования
- Кнопки без try/except
- Краши без понятного сообщения об ошибке

## Quality Gates

### Перед завершением любой задачи
1. [ ] `pytest` проходит без ошибок (326 тестов)
2. [ ] Self-review на типичные ошибки
3. [ ] Нет секретов в логах
4. [ ] Все ресурсы закрываются (async with, try/finally)
5. [ ] Type hints на всех публичных функциях
6. [ ] Docstrings с Args/Returns

### Code Standards
- Type hints required for all function parameters and return values
- Use `Optional[T]` for nullable types
- Prefer `Path` over string paths
- Use dataclasses for structured data
- Async context managers for resource management

## Development Patterns

### Resource Cleanup Pattern (ОБЯЗАТЕЛЬНО для любого нового кода)
- proxy_relay: always cleanup in try/finally or outer except (see browser_manager.py FIX-A)
- subprocess: always kill in except/finally (see proxy_relay.py FIX-B)
- BrowserManager: close_all() in finally of any method that creates browsers
- asyncio.Queue: task_done() MUST be in finally block, never after try/except
- GUI batch ops: disable buttons + check _active_pool guard before starting
- CancelledError: use `except BaseException` (not Exception) when cleaning up resources in async code
- Batch pause: always `set()` batch_pause_event before sending stop sentinels to workers

### Windows Gotchas
- NEVER use `taskkill /IM` — kills ALL instances including parallel workers
- Use psutil for PID-based process killing (cross-platform)
- `tail` command doesn't work natively in cmd — use Read tool instead
- PowerShell commands (`Get-ChildItem`) don't work in bash tool — use `ls` / `find`
- File paths with Cyrillic need quotes in bash commands

### Code Review Checklist (after every fix)
- Double-cleanup: if resource cleaned in inner except, set to None to prevent outer cleanup
- All batch entry points need _active_pool guard (migrate_selected, migrate_all, fragment_all)
- proxy_relay.stop() is idempotent but avoid calling twice for clarity

## Known Issues

### Direct Session Injection (НЕ РАБОТАЕТ)
Telegram Web K валидирует сессии на сервере. Единственный рабочий путь - **Programmatic QR Login**.

### Resource Leaks (all P0 fixed as of 2026-02-09)
All critical resource leaks fixed: PID-based kill (psutil), proxy_relay cleanup (FIX-A/B),
worker pool cleanup (FIX-C/D), GUI guards (FIX-F/G), shutdown handler (atexit+signal+psutil).

### Pre-prod Audit Fixes (2026-02-12, commit ee5957b)
- FloodWait detection: case-insensitive match in worker_pool.py
- CancelledError: `except BaseException` in browser_manager.py launch()
- Batch pause deadlock: `batch_pause_event.set()` before stop sentinels
- Dedup: `dict.fromkeys()` in migrate_accounts_parallel()
- Min cooldown: `max(cooldown, MIN_COOLDOWN)` in ParallelMigrationController
- GUI shutdown: poll _active_pool for graceful worker completion

### Fragment Auth
- Live verified 1/1 (commit 3388c58), CSS проверены через Playwright MCP
- Fallback по text content для устойчивости к UI-изменениям
- Ready for canary (10 аккаунтов)

### Unfixed Bugs (P2/P3)
- FIX-005: 2FA selector hardcoded
- find_free_port TOCTOU race (proxy_relay.py:61) — mitigated by health check + retry

## Packaging (PyInstaller EXE)

### Сборка дистрибутива
```bash
pip install pyinstaller
python build_exe.py         # -> dist/TGWebAuth.zip (~400MB)
```

### Frozen exe особенности
- `src/paths.py`: `sys.frozen` → `sys.executable.parent` (вместо `__file__`)
- `src/proxy_relay.py`: in-process pproxy (no subprocess) when frozen
- `src/browser_manager.py`: `executable_path` → `APP_ROOT/camoufox/camoufox.exe` when frozen
- Camoufox binary copied by `build_exe.py` into `dist/TGWebAuth/camoufox/`

## Available MCP Tools (preserve after compaction)

**ALWAYS use these tools proactively:**
- **Serena** — symbolic code editing: `find_symbol`, `replace_symbol_body`, `search_for_pattern`, `get_symbols_overview`
- **Context7** — library docs lookup: `resolve-library-id` → `query-docs`
- **Playwright MCP** — browser automation: `browser_snapshot`, `browser_click`, `browser_navigate`, `browser_take_screenshot`
- **Tavily** — web search/extract: `tavily_search`, `tavily_extract`, `tavily_research`
- **Filesystem MCP** — file operations: `read_text_file`, `write_file`, `edit_file`, `directory_tree`
