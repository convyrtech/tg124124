# TG Web Auth Migration Tool

## MANDATORY: Read Before ANY Work
**ALWAYS read `.claude/MASTER_PROMPT.md` at the start of EVERY session or after context loss.**
It contains: project context, all available tools, work methodology, safety rules, progress tracker, and context recovery protocol.

**Key documents:**
- `.claude/MASTER_PROMPT.md` - Master operating guide (READ FIRST)
- `docs/ACTION_PLAN_2026-02-08.md` - Current implementation plan
- `PROMPTS.md` - Prompt generator and plugin guide

## Project Goal
Автоматическая миграция Telegram session файлов (Telethon) в браузерные профили для:
- **web.telegram.org** - основной веб-клиент (QR Login)
- **fragment.com** - NFT usernames, Telegram Stars (OAuth popup)

Масштаб: **1000 аккаунтов**, переносимость между ПК.

## Current Status (2026-02-08)

### Что работает
- Programmatic QR Login (без ручного сканирования)
- Multi-decoder QR (jsQR, OpenCV, pyzbar)
- Camoufox antidetect browser с persistent profiles
- SOCKS5 proxy с auth через pproxy relay
- Batch миграция (sequential + parallel через ParallelMigrationController)
- Fragment.com OAuth popup flow (переписан, требует тестирования на реальных аккаунтах)
- SQLite metadata (accounts, proxies, migrations, batches, operation_log)
- Worker pool (asyncio queue, retry, circuit breaker) - реализован, интегрирован в GUI
- Profile lifecycle (hot/cold tiering, LRU eviction, zip compression)
- Proxy management (import, health check, auto-replace dead)
- Auth TTL 365 days (SetAuthorizationTTLRequest после миграции)
- Resource monitor (CPU/RAM, adaptive concurrency)
- GUI (DearPyGui, 80% complete)
- CLI: 9 команд (migrate, open, list, check, health, fragment, check-proxies, proxy-refresh, init)
- 255 тестов проходят

### Что НЕ работает / НЕ доделано
- **Resource leaks** - зомби-процессы pproxy/Camoufox при таймаутах/крашах
- **Fragment auth** - переписан, но CSS-селекторы не проверены на реальном сайте, 11 багов из аудита
- **Worker pool не в CLI** - CLI использует ParallelMigrationController, а не worker_pool.py
- **FIX-001..007** - QR decode grey zone, SQLite lock в parallel, зависания без таймаутов
- **GUI polish** - запускается, но не все кнопки протестированы
- **migration_state.py** - deprecated, но CLI всё ещё импортирует для --resume/--retry-failed

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

## File Structure (9366 строк src/, 255 тестов)
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
├── src/                     # 9366 строк
│   ├── telegram_auth.py     # QR auth + AcceptLoginToken (2036 строк)
│   ├── fragment_auth.py     # Fragment.com OAuth popup (655 строк)
│   ├── browser_manager.py   # Camoufox + ProfileLifecycleManager (718 строк)
│   ├── worker_pool.py       # Asyncio queue pool (572 строк, GUI only)
│   ├── cli.py               # CLI 9 команд (978 строк)
│   ├── database.py          # SQLite: accounts, proxies, migrations (905 строк)
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
│       ├── app.py           # DearPyGui main window (1224 строк)
│       ├── controllers.py   # GUI business logic (278 строк)
│       └── theme.py         # Hacker dark green theme (99 строк)
├── tests/                   # 255 тестов
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
- **OpenCV/pyzbar/jsQR** - QR decoding (3 fallback decoders)
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
pytest                    # Все 255 тестов
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
1. [ ] `pytest` проходит без ошибок (255 тестов)
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

## Known Issues

### Direct Session Injection (НЕ РАБОТАЕТ)
Telegram Web K валидирует сессии на сервере. Единственный рабочий путь - **Programmatic QR Login**.

### Resource Leaks (КРИТИЧНО для 1000 аккаунтов)
- proxy_relay не закрывается при TimeoutError browser launch
- pproxy process leak при race condition
- Нет shutdown handler для дочерних процессов

### Fragment Auth (требует тестирования)
- CSS-селекторы не проверены на реальном fragment.com
- asyncio.Event race condition в GUI context
- Regex ловит любые 5-6 цифр как код подтверждения

### Unfixed Bugs (FIX-001..007)
- QR decode grey zone (len check)
- SQLite "database is locked" в parallel mode
- Telethon connect() зависает 180s без timeout
- Browser launch зависает без timeout
- 2FA selector hardcoded
