# TG Web Auth Migration Tool

## MANDATORY: Read Before ANY Work
**ALWAYS read `.claude/MASTER_PROMPT.md` at the start of EVERY session or after context loss.**
It contains: project context, all available tools, work methodology, safety rules, progress tracker, and context recovery protocol.

**Key documents:**
- `.claude/MASTER_PROMPT.md` - Master operating guide (READ FIRST)
- `docs/plans/2026-02-09-production-1000-design.md` - **CURRENT** production plan (Phase 1 done, Phase 2 next)
- `docs/ACTION_PLAN_2026-02-08.md` - Legacy plan (partially superseded by production plan above)
- `PROMPTS.md` - Prompt generator and plugin guide

## Project Goal
Автоматическая миграция Telegram session файлов (Telethon) в браузерные профили для:
- **web.telegram.org** - основной веб-клиент (QR Login)
- **fragment.com** - NFT usernames, Telegram Stars (OAuth popup)

Масштаб: **1000 аккаунтов**, переносимость между ПК.

## Current Status (2026-02-09)

### Что работает
- Programmatic QR Login (без ручного сканирования)
- Multi-decoder QR (jsQR, OpenCV, pyzbar)
- Camoufox antidetect browser с persistent profiles
- SOCKS5 proxy с auth через pproxy relay
- Batch миграция (sequential + parallel через ParallelMigrationController)
- Fragment.com OAuth popup flow (переписан, требует тестирования на реальных аккаунтах)
- SQLite metadata (accounts, proxies, migrations, batches, operation_log) + WAL + busy_timeout=30s
- Worker pool (asyncio queue, retry, circuit breaker, mode web/fragment) - интегрирован в GUI
- Shared BrowserManager в worker pool (LRU eviction работает глобально)
- Profile lifecycle (hot/cold tiering, LRU eviction, zip compression)
- Proxy management (import, health check, auto-replace dead)
- Auth TTL 365 days (SetAuthorizationTTLRequest после миграции)
- Resource monitor (CPU/RAM, adaptive concurrency)
- PID-based force-kill zombie browsers (psutil, не taskkill /IM)
- QR decode: zxing-cpp + morphological preprocessing (100% rate на dot-style и thin-line QR)
- GUI (DearPyGui, 90% complete): Migrate All, Retry Failed, Fragment All, STOP, progress throttle, fragment_status column
- CLI: 9 команд (migrate, open, list, check, health, fragment, check-proxies, proxy-refresh, init)
- 269 тестов проходят

### Что НЕ работает / НЕ доделано
- **Fragment auth** - CSS-селекторы не проверены на реальном fragment.com
- **Worker pool не в CLI** - CLI использует ParallelMigrationController, а не worker_pool.py
- **FIX-005** - 2FA selector hardcoded
- **GUI polish** - кнопки работают, нужно ручное тестирование на реальных аккаунтах
- **migration_state.py** - deprecated, не используется (можно удалить)

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

## File Structure (9565 строк src/, 269 тестов)
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
├── src/                     # 9565 строк
│   ├── telegram_auth.py     # QR auth + AcceptLoginToken (2038 строк)
│   ├── fragment_auth.py     # Fragment.com OAuth popup + fragment_account() (689 строк)
│   ├── browser_manager.py   # Camoufox + ProfileLifecycleManager + PID kill (761 строк)
│   ├── worker_pool.py       # Asyncio queue pool, mode web/fragment (622 строк)
│   ├── cli.py               # CLI 9 команд (978 строк)
│   ├── database.py          # SQLite: accounts, proxies, migrations, WAL (907 строк)
│   ├── proxy_manager.py     # Import, health check, auto-replace (442 строк)
│   ├── proxy_relay.py       # SOCKS5→HTTP relay via pproxy (280 строк)
│   ├── proxy_health.py      # Batch TCP check (103 строк)
│   ├── resource_monitor.py  # CPU/RAM monitoring (159 строк)
│   ├── security_check.py    # Fingerprint/WebRTC check (372 строк)
│   ├── migration_state.py   # DEPRECATED JSON state (321 строк)
│   ├── utils.py             # Proxy parsing helpers (103 строк)
│   ├── logger.py            # Logging setup (83 строк)
│   ├── pproxy_wrapper.py    # pproxy process (23 строк)
│   └── gui/
│       ├── app.py           # DearPyGui main window (1292 строк)
│       ├── controllers.py   # GUI business logic (278 строк)
│       └── theme.py         # Hacker dark green theme (99 строк)
├── tests/                   # 269 тестов
│   ├── test_telegram_auth.py
│   ├── test_fragment_auth.py
│   ├── test_browser_manager.py
│   ├── test_proxy_manager.py
│   ├── test_proxy_health.py
│   ├── test_proxy_relay.py
│   ├── test_worker_pool.py
│   ├── test_resource_monitor.py
│   ├── test_database.py
│   ├── test_migration_state.py
│   ├── test_integration.py
│   ├── test_utils.py
│   └── conftest.py
├── scripts/                 # Эксперименты (не в git, dead code)
├── docs/                    # Документация
├── decode_qr.js             # Node.js jsQR decoder
├── package.json
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
- **zxing-cpp/OpenCV/pyzbar/jsQR** - QR decoding (zxing-cpp primary, 3 fallbacks)
- **Click** - CLI
- **aiosqlite** - Async SQLite
- **DearPyGui** - GUI
- **psutil** - Resource monitoring

## Commands

### Установка
```bash
pip install -r requirements.txt
npm install                  # для jsQR decoder
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
pytest                    # Все 269 тестов
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
- Bare `except:` (только `except Exception as e:`)
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
1. [ ] `pytest` проходит без ошибок (269 тестов)
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

### Fragment Auth (требует тестирования)
- CSS-селекторы не проверены на реальном fragment.com
- asyncio.Event race condition в GUI context
- Regex ловит любые 5-6 цифр как код подтверждения

### Unfixed Bugs
- FIX-005: 2FA selector hardcoded
