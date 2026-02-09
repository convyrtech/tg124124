# TG Web Auth - Action Plan v3

**Date:** 2026-02-08
**Goal:** Production-ready migration of 1000 Telegram sessions
**Status:** Core infrastructure built, need stabilization before production

---

## What's DONE (not in plan anymore)

| Feature | When | Where |
|---------|------|-------|
| Programmatic QR Login | v1 | telegram_auth.py |
| Multi-decoder QR (jsQR, OpenCV, pyzbar) | v1 | telegram_auth.py |
| Camoufox antidetect + persistent profiles | v1 | browser_manager.py |
| SOCKS5 proxy relay via pproxy | v1 | proxy_relay.py |
| Batch migration (sequential) | v1 | telegram_auth.py |
| CLI: migrate, open, list, check, init, health | v1 | cli.py |
| SQLite state: accounts, proxies, migrations, batches, operation_log | Feb 6 | database.py |
| Deprecate migration_state.py (JSON) -> SQLite | Feb 6 | database.py |
| Worker pool (asyncio queue, retry, circuit breaker) | Feb 6 | worker_pool.py |
| Profile lifecycle (hot/cold, LRU, zip) | Feb 6 | browser_manager.py |
| Auth TTL 365 days (SetAuthorizationTTLRequest) | Feb 6 | telegram_auth.py |
| Proxy health check (batch TCP, 50 concurrent) | Feb 6 | proxy_health.py |
| CLI: check-proxies | Feb 6 | cli.py |
| Fragment.com OAuth popup rewrite | Feb 6 | fragment_auth.py |
| CLI: fragment --account / --all | Feb 6 | cli.py |
| Resource monitor (CPU/RAM, adaptive concurrency) | Feb 6 | resource_monitor.py |
| ParallelMigrationController (CLI parallel mode) | Feb 6 | telegram_auth.py |
| GUI: DearPyGui (accounts, proxies, logs, settings) | Feb 6 | gui/ |
| Proxy pool management (import, health, replace dead) | Feb 8 | proxy_manager.py |
| CLI: proxy-refresh (auto-replace dead proxies) | Feb 8 | cli.py |
| 269 tests passing | Feb 9 | tests/ |

---

## What's LEFT

### Phase A: Stabilization (BLOCKER for production)

Без этого нельзя запускать 1000 аккаунтов - система упадёт от утечек ресурсов.

#### A.1: Resource Leaks Fix
**Files:** `browser_manager.py`, `proxy_relay.py`, `telegram_auth.py`
**Priority:** P0 | **Risk:** System crash at ~50 accounts

- [x] Timeout на browser launch (max 60s) — уже было в worker_pool
- [x] proxy_relay cleanup при TimeoutError / crash — FIX-A (outer try/except) + FIX-B (health check kill)
- [x] pproxy process PID tracking + force kill — psutil PID-based kill в browser_manager.py
- [x] Shutdown handler — FIX-C (pool finally closes BrowserManager) + FIX-D (task_done finally)
- [x] Timeout на Telethon connect() (max 30s) — уже было в telegram_auth
- [x] Timeout на page.goto() (max 30s) — уже было в telegram_auth
- [x] Тесты: timeout, crash, cleanup scenarios — 14 новых тестов
- [x] Run pytest - all pass — 269 тестов

#### A.2: Fix Critical Bugs
**Files:** `telegram_auth.py`, `browser_manager.py`
**Priority:** P0

- [ ] FIX-001: QR decode grey zone (len<500 check wrong)
- [x] FIX-002: SQLite "database is locked" in parallel (WAL + busy_timeout) — DONE: PRAGMA journal_mode=WAL + busy_timeout=30000 в database.py
- [ ] FIX-005: 2FA selector hardcoded, visibility not checked
- [x] FIX-006: Telethon connect() hangs 180s — уже есть timeout в telegram_auth
- [x] FIX-007: Browser launch hangs — уже есть timeout в telegram_auth
- [x] Cooldown: 60-120s random jitter — уже в worker_pool.py
- [x] Batch pauses: after 10 accounts, pause 5-10 min — уже в worker_pool.py
- [x] Тесты — 269 тестов проходят
- [x] Run pytest - all pass

**NOTE:** FIX-003 (lock files) и FIX-004 (JSON race) больше не актуальны - JSON state deprecated, используем SQLite WAL.

#### A.3: Integrate Worker Pool in CLI
**Files:** `cli.py`, `worker_pool.py`
**Priority:** P1

CLI сейчас использует ParallelMigrationController (semaphore-based), а worker_pool.py (queue-based с retry/circuit breaker) используется только в GUI.

- [ ] Заменить ParallelMigrationController на MigrationWorkerPool в CLI --parallel
- [ ] Или: оставить оба, но убедиться что оба работают корректно
- [ ] Убрать dead code: migrate_accounts_parallel(), если не используется
- [ ] Тесты
- [ ] Run pytest - all pass

#### A.4: Delete Dead Code
**Files:** various
**Priority:** P2

- [ ] Удалить migration_state.py (deprecated, CLI --resume/--retry-failed переписать на SQLite)
- [ ] Удалить tests/test_migration_state.py
- [ ] Удалить scripts/ (inject_session, session_converter, extract_tg_storage - всё dead)
- [ ] Удалить poc_qr_login.py (root, prototype)
- [ ] Удалить дублирование proxy parsing (telegram_auth vs utils vs gui/controllers vs proxy_manager)
- [ ] Run pytest - all pass

---

### Phase B: Fragment.com

#### B.1: Verify & Fix Fragment Auth on Real Site
**Files:** `fragment_auth.py`
**Priority:** P0 for Fragment

- [ ] Открыть fragment.com через Playwright MCP
- [ ] Скриншот каждого шага OAuth flow
- [ ] Проверить ВСЕ CSS-селекторы (button.login-link, .login-form, etc.)
- [ ] Исправить 11 багов из docs/TODO_FRAGMENT_FIXES.md
- [ ] Добавить retry с exponential backoff
- [ ] Проверить на реальном аккаунте
- [ ] Тесты
- [ ] Run pytest - all pass

---

### Phase C: Pre-flight & Production

#### C.1: Pre-flight Checks
**Priority:** P0 before production

- [ ] Batch check: все 1000 Telethon sessions alive (get_me())
- [ ] Batch check: все прокси alive (proxy_health)
- [ ] Verify: 1:1 proxy-account mapping (no sharing)
- [ ] Report: X alive sessions, Y alive proxies, Z ready
- [ ] CLI command: `python -m src.cli preflight`

#### C.2: Canary Migration (5-10 accounts)
**Priority:** P0

- [ ] Выбрать 5-10 "расходных" аккаунтов
- [ ] Полная миграция с мониторингом
- [ ] Проверить: профиль работает, сессия жива 24+ часов
- [ ] Проверить: нет банов, FLOOD_WAIT, AUTH_KEY errors
- [ ] Задокументировать результаты

#### C.3: Production Migration (990 accounts)
**Priority:** P0

- [ ] Батчами по 50-100 аккаунтов в день
- [ ] Мониторинг FLOOD_WAIT и банов
- [ ] Ожидаемый срок: 3-5 дней
- [ ] Ежедневная проверка здоровья готовых аккаунтов

#### C.4: Keep-Alive Setup
**Files:** new module or cron
**Priority:** P1

- [ ] Weekly batch keep-alive через Telethon `updates.getState`
- [ ] Без браузера - только API calls
- [ ] CLI command: `python -m src.cli keepalive --all`

---

### Phase D: GUI Polish (can be parallel)

#### D.1: GUI Testing & Polish
**Files:** `gui/app.py`, `gui/controllers.py`
**Priority:** P2

- [ ] Запуск и тестирование каждой кнопки
- [ ] Progress bar для batch операций
- [ ] Proxy display в таблице аккаунтов
- [ ] Cooldown slider (60-120s)
- [ ] Canary mode toggle
- [ ] Fragment auth button verification

---

## Execution Order

```
Phase A: Stabilization        [~3-4 дня]  БЛОКЕР
  A.1: Resource leaks fix     [1-2 дня]
  A.2: Critical bugs fix      [1 день]
  A.3: Worker pool in CLI     [0.5 дня]
  A.4: Dead code cleanup      [0.5 дня]

Phase B: Fragment             [~2 дня]    Параллельно с C.1
  B.1: Verify on real site    [2 дня]

Phase C: Production           [~5-7 дней]
  C.1: Pre-flight checks      [0.5 дня]
  C.2: Canary (5-10 accounts) [1 день]
  C.3: Production (990 accs)  [3-5 дней]
  C.4: Keep-alive setup       [0.5 дня]

Phase D: GUI Polish           [~1-2 дня]  Параллельно
                               ──────────
                               ~10-13 дней total
```

---

## Safety Rules (unchanged)

- Max 5-8 parallel browsers (16GB RAM)
- Cooldown: 60-120s random jitter per account
- Batch pauses: 5-10 min every 10 accounts
- 1 dedicated SOCKS5 proxy per account
- NEVER same session from 2 clients
- Set auth TTL to 365 days after migration
- Keep-alive: weekly Telethon API call
- Canary FIRST, production AFTER 24h stability

## Success Criteria

- [x] 269 tests pass
- [ ] No zombie processes after 100 sequential migrations
- [ ] Canary: 5-10 accounts stable 24+ hours
- [ ] Production: 1000 accounts migrated
- [ ] Fragment.com auth working on real site
- [ ] Weekly keep-alive running
- [ ] No secrets in logs
