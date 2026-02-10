# Production-Ready: 1000 аккаунтов — Финальный план

> **Status:** Research complete, ready for implementation
> **Date:** 2026-02-09
> **Goal:** Нажать 2 кнопки в GUI → 1000 аккаунтов мигрированы + авторизованы на fragment.com. Стабильно, без крашей, на 16GB RAM.

---

## Table of Contents

1. [Overview](#overview)
2. [Resource Leaks — MUST FIX](#1-resource-leaks--must-fix)
3. [Proxy Relay Lifecycle](#2-proxy-relay-lifecycle)
4. [Fragment Auth — Ship or Skip](#3-fragment-auth--ship-or-skip)
5. [Dual Migration System](#4-dual-migration-system)
6. [GUI Race Conditions](#5-gui-race-conditions)
7. [Security — Pragmatic Baseline](#6-security--pragmatic-baseline)
8. [Testing Strategy](#7-testing-strategy)
9. [Dead Code Cleanup](#8-dead-code-cleanup)
10. [Context Persistence](#9-context-persistence)
11. [Implementation Plan](#implementation-plan)

---

## Overview

### Глобальная цель

Безопасная и быстрая миграция 1000 Telegram аккаунтов:
1. **web.telegram.org** — QR Login через Camoufox (РАБОТАЕТ)
2. **fragment.com** — OAuth popup (НЕ ПРОВЕРЕНО на реальном сайте)

### Аудит выявил

| Категория | Найдено | Блокирует продакшн? |
|-----------|---------|---------------------|
| Resource leaks | 22 утечки | **ДА** — crash на ~200-400 акков |
| Security | 31 finding (4 CRITICAL) | Нет — десктоп-приложение |
| Dead code | ~500 строк deprecated | Нет |
| Architecture | 2 параллельные системы | Нет — GUI использует правильную |
| Fragment auth | 11 багов, 0 CSS проверено | Нет — можно запустить ПОСЛЕ web |
| GUI | Race condition double-click | **ДА** — orphaned pool → OOM |
| Tests | 269, но 0% CLI/GUI | Нет — smoke test достаточно |

### Ключевые решения

| Аспект | Решение | Обоснование |
|--------|---------|-------------|
| Resource leaks | Fix 5 P0 leaks | Crash на ~200-400 акков без них |
| Proxy relay | try/finally в 2 местах | #1 source of zombie processes |
| Fragment auth | **ПОСЛЕ** web миграции | Bounded context, не блокирует web |
| Dual system | Игнорировать, GUI работает | Refactoring ≠ value right now |
| GUI race | Guard + disable buttons | 15 минут работы, предотвращает OOM |
| Security | Fix pproxy CLI args only | Desktop app, не сервер |
| Testing | Smoke test 10 акков + мониторинг | Тесты логики есть, нужна валидация масштаба |
| Dead code | После canary | Не constraint, не блокирует |
| Context | CLAUDE.md trigger + MASTER_PROMPT | Уже работает, не усложнять |

---

## 1. Resource Leaks — MUST FIX

> **Experts:** Werner Vogels (reliability), Brendan Gregg (systems perf)

### Crash-point estimates

| Leak | RAM per instance | Crash at account # | Priority |
|------|-----------------|-------------------|----------|
| Proxy relay not stopped on 1st timeout | 50MB | ~250 | **P0** |
| Browser PID kill fails on Windows | 500MB | ~200 | **P0** |
| Worker pool task timeout orphans browser | 600MB | ~400 | **P0** |
| Worker pool не закрывает BrowserManager | 500MB×N | При STOP+restart | **P0** |
| Queue task_done() not in finally | Deadlock | 1 exception = hang | **P0** |

### 5 обязательных фиксов

**FIX-A: proxy_relay cleanup on first browser timeout**
```
Файл: src/browser_manager.py:546-548
Добавить: if proxy_relay: await proxy_relay.stop()
Строк: 4
```

**FIX-B: proxy_relay.start() — kill subprocess on health check failure**
```
Файл: src/proxy_relay.py:131-159
Обернуть health check в try/except, kill process on failure
Строк: 12
```

**FIX-C: worker_pool — BrowserManager cleanup в finally**
```
Файл: src/worker_pool.py:183 (run method)
Добавить: try/finally с browser_manager.close_all()
Строк: 6
```

**FIX-D: worker_pool — task_done() в finally**
```
Файл: src/worker_pool.py:271
Перенести self._queue.task_done() в finally блок
Строк: 3
```

**FIX-E: taskkill /PID вместо /IM как fallback**
```
Файл: src/browser_manager.py:451-466
Заменить: taskkill /IM camoufox.exe → taskkill /PID {pid} /F /T
Строк: 5
```

### Что ИГНОРИРУЕМ

- LEAK-007 (hibernate PermissionError) — disk issue, не RAM OOM
- LEAK-012 (debug screenshots) — 50MB max, trivial
- LEAK-018 (circuit breaker reset) — 60s delay, не crash
- LEAK-019 (SQLite single connection) — WAL + busy_timeout уже есть
- LEAK-020 (thread join timeout) — rare, only on GUI close

---

## 2. Proxy Relay Lifecycle

> **Expert:** Brendan Gregg (systems performance)

### Два точных изменения

**Изменение 1:** `browser_manager.py` — обернуть весь launch в try/except для relay cleanup

После `proxy_relay.start()` (строка 516), любое исключение в launch flow должно остановить relay:

```python
# Wrap lines 539-598 in try/except
try:
    camoufox = AsyncCamoufox(**args)
    # ... existing launch + retry code ...
    return ctx
except Exception:
    if proxy_relay:
        try:
            await proxy_relay.stop()
        except Exception as e:
            logger.warning("Relay cleanup error: %s", e)
    raise
```

**Изменение 2:** `proxy_relay.py:start()` — kill subprocess если health check fails

```python
self._process = await asyncio.create_subprocess_exec(...)
try:
    # ... health check loop ...
except Exception:
    if self._process:
        self._process.kill()
        self._process = None
    raise
```

### Idempotency

`ProxyRelay.stop()` уже idempotent (проверяет `if self._process`). Double-stop безопасен.

---

## 3. Fragment Auth — Ship or Skip

> **Expert:** Sam Newman (bounded contexts)

### Решение: Ship web FIRST, Fragment LATER

**Обоснование:**
- Web auth РАБОТАЕТ и протестирован → ready for 1000 accounts
- Fragment auth: 0 CSS селекторов проверено, 4 CRITICAL бага, 0 live tests
- Fragment НЕЗАВИСИМ от web — может запускаться на уже мигрированных профилях
- Coupling = shared blast radius → если Fragment сломается, web тоже падает

### Последовательность

```
Неделя 1:
  День 1-2: Fix 5 P0 resource leaks + GUI race condition
  День 3:   Canary web migration (5-10 аккаунтов)
  День 4-6: Production web migration (990 аккаунтов, батчами)

Неделя 2:
  День 1:   Fix 4 CRITICAL Fragment bugs
  День 2:   Live test fragment.com (1 аккаунт, DevTools, fix CSS)
  День 3:   Canary Fragment (10 web-мигрированных профилей)
  День 4-5: Production Fragment (990 профилей)
```

### Fragment критические баги (для Недели 2)

1. Regex `r'\b(\d{5,6})\b'` ловит любые числа → убрать или добавить keyword guard
2. SQLite connection leak в WAL setup → использовать `with sqlite3.connect() as conn:`
3. Phone leak в логах → `_mask_phone(str(e))`
4. Все CSS селекторы → проверить DevTools на реальном fragment.com

---

## 4. Dual Migration System

> **Expert:** Martin Fowler (refactoring)

### Решение: Игнорировать, консолидировать ПОСЛЕ продакшна

- GUI использует MigrationWorkerPool (622 строки, retry, circuit breaker, batch pause)
- CLI использует ParallelMigrationController (200 строк, basic)
- User будет использовать GUI → worker pool уже работает
- Refactoring = 2-4 часа, не добавляет value для 1000 акков через GUI

**Действие:** Добавить комментарий `# LEGACY: Use MigrationWorkerPool (GUI) for production` в ParallelMigrationController.

---

## 5. GUI Race Conditions

> **Expert:** Don Norman (error prevention)

### Решение: Guard + disable buttons

**FIX-F: _migrate_all() guard**
```python
def _migrate_all(self, sender=None, app_data=None) -> None:
    if self._active_pool:
        self._log("[Migrate] Migration already in progress")
        return
    # ... rest
```

**FIX-G: Disable batch buttons during operation**
```python
def _set_batch_buttons_enabled(self, enabled: bool):
    for tag in ["btn_migrate_all", "btn_migrate_selected", "btn_fragment_all"]:
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, enabled=enabled)
```

Добавить `tag=` к кнопкам + вызывать `_set_batch_buttons_enabled(False)` при старте, `True` в finally.

---

## 6. Security — Pragmatic Baseline

> **Expert:** Troy Hunt (threat modeling)

### Решение: Fix только pproxy CLI args

**Threat model:** Single-user desktop app. Если атакующий имеет доступ к файлам — .session файлы уже скомпрометированы (Telegram сам хранит их plaintext SQLite).

**FIX-H: pproxy credentials via env var** (опционально, P2)
```
Файл: src/proxy_relay.py:120-125
Вместо: cmd = [..., "-r", remote_uri]  # credentials visible in tasklist
Использовать: env var или stdin pipe
```

**Что НЕ делаем:**
- Шифрование SQLite (security theater — 10 других файлов plaintext)
- Windows Credential Manager (overkill для solo developer)
- File permissions (Windows default = user-only если не shared)

---

## 7. Testing Strategy

> **Expert:** Kent C. Dodds (testing confidence)

### Решение: Smoke test + мониторинг вместо 50+ unit tests

269 тестов покрывают логику. Нам нужна **валидация масштаба**, не coverage.

**Smoke test протокол (перед 1000 акков):**

1. Запустить GUI: `python -m src.gui.app`
2. "Migrate All" на 10 тестовых аккаунтов
3. Мониторить:
   - `tasklist | findstr camoufox` — 0 zombie после завершения
   - `tasklist | findstr pproxy` — 0 zombie
   - RAM usage в Task Manager — стабильно, не растёт
4. Нажать STOP на 5-м аккаунте → проверить cleanup
5. Закрыть GUI во время миграции → проверить cleanup
6. Повторить → нет накопления zombie

**Если smoke test пройден** → production 1000 акков.
**Если нет** → fix конкретный leak, повторить.

---

## 8. Dead Code Cleanup

> **Expert:** Gene Kim (theory of constraints)

### Решение: После canary, не сейчас

Dead code (migration_state.py, scripts/, temp files) **не является constraint**. Не вызывает crashes, не блокирует миграцию.

**После canary:**
- Удалить `src/migration_state.py` + `tests/test_migration_state.py`
- Удалить `scripts/` directory
- Удалить root temp files (popup_debug_846.html, --db-path, etc.)
- Удалить unused imports (math, os)

---

## 9. Context Persistence

> **Expert:** Kelsey Hightower (infrastructure pragmatism)

### Решение: Усилить текущий подход (CLAUDE.md trigger)

CLAUDE.md **автоматически читается** при каждой сессии (via system-reminder). MASTER_PROMPT.md — нет.

**Действие:** Убедиться что CLAUDE.md содержит чёткую инструкцию:
```markdown
## MANDATORY: Read Before ANY Work
**ALWAYS read `.claude/MASTER_PROMPT.md` at the start of EVERY session.**
```

Это уже есть. Дополнительно:
- Обновлять MASTER_PROMPT Progress Tracker после каждого шага
- Держать ACTION_PLAN актуальным с чекбоксами
- НЕ усложнять: subagent memory, agent teams, Tasks API — overkill для solo developer

---

## Implementation Plan

### Phase 1: Critical Fixes (День 1-2)

- [x] **FIX-A**: proxy_relay cleanup on ANY exception in browser launch (`browser_manager.py`) — expanded: outer try/except catches non-timeout errors too
- [x] **FIX-B**: proxy_relay.start() kill subprocess on failure (`proxy_relay.py`)
- [x] **FIX-C**: worker_pool BrowserManager cleanup in finally (`worker_pool.py`)
- [x] **FIX-D**: task_done() в finally block (`worker_pool.py`)
- [x] **FIX-E**: Removed dangerous taskkill /IM fallback (`browser_manager.py`) — replaced with warning log
- [x] **FIX-F**: _migrate_all() + _migrate_selected() pool guard (`gui/app.py`)
- [x] **FIX-G**: Disable batch buttons during operation (`gui/app.py`) — added tags + _set_batch_buttons_enabled()
- [x] pytest — все 269 тестов зелёные
- [x] Code review: fixed double proxy_relay.stop(), added missing guard in _migrate_selected()

### Phase 1.7: Relay Recreation + CLI Shutdown (2026-02-10)

- [x] browser_manager: proxy relay recreation on retry (stop old → new ProxyRelay → fresh port)
- [x] cli.py: atexit orphan killer (psutil cmdline for pproxy, name for camoufox/firefox)
- [x] cli.py: KeyboardInterrupt handler for fragment --all batch mode
- [x] Audit: found & fixed pproxy cmdline detection bug (python.exe -m pproxy)
- [x] 2 new tests for relay recreation (271 total)
- [x] Removed unused `needs_relay` import from tests

### Phase 1.5: Quality & UX Fixes (team audit findings)

- [x] requirements.txt: добавлены dearpygui, aiosqlite, screeninfo
- [x] FLOOD_WAIT лимит 300s → 3600s в telegram_auth.py
- [x] Унификация путей: GUI sessions_dir → accounts/ (как CLI)
- [x] Авто-скачивание шрифта JetBrainsMono при первом запуске GUI
- [x] 2FA диалог переведён на русский + "Пропустить"
- [x] Signal handler (SIGINT) + psutil child kill в _shutdown()
- [x] CLI: reset_interrupted_migrations() при --resume/--all
- [x] humanize_error(): маппинг 12 технических ошибок → русский
- [x] Cooldown лог: "Пауза Xс между аккаунтами (антибан)..."
- [x] Code review: humanize_error Optional fix, sys import, SIGTERM Windows guard
- [x] pytest — 269 тестов зелёные

### Phase 1.6: Data Integrity & UX Polish

- [x] fragment_status добавлен в AccountRecord dataclass
- [x] get_account() и list_accounts() читают fragment_status из БД
- [x] GUI: колонка "Fragment" в таблице аккаунтов (authorized/-)
- [x] GUI: статистика fragment_authorized в header ([F] count)
- [x] GUI: "Fragment All" пропускает уже авторизованные аккаунты
- [x] GUI: кнопка "Retry Failed" — повтор всех error аккаунтов
- [x] GUI: _fragment_single() — 3 бага: path .parent, proxy из БД, update fragment_status
- [x] GUI: get_stats() включает fragment_authorized count
- [x] CLAUDE.md: обновлены Known Issues и текущий статус
- [x] migration_state.py: CLI больше не импортирует (можно удалить)
- [x] pytest — 269 тестов зелёные

### Phase 2: Smoke Test (День 3)

- [ ] 10 тестовых аккаунтов через GUI "Migrate All"
- [ ] Мониторинг RAM/processes
- [ ] STOP mid-batch → verify cleanup
- [ ] Close GUI mid-batch → verify cleanup
- [ ] 0 zombie processes после всех тестов

### Phase 3: Production Web Migration (День 4-6)

- [ ] Batch 1: 50 аккаунтов
- [ ] Check: RAM stable, 0 zombies, no FLOOD_WAIT
- [ ] Batch 2-19: по 50 аккаунтов (950 total)
- [ ] Daily health check готовых аккаунтов

### Phase 4: Fragment Auth Fix (День 7-8)

- [ ] Fix 4 CRITICAL Fragment bugs
- [ ] Live test fragment.com в DevTools
- [ ] Fix CSS selectors по реальному DOM
- [ ] Canary: 10 аккаунтов
- [ ] Production: 990 аккаунтов

### Phase 5: Cleanup (День 9)

- [ ] Delete dead code (migration_state.py, scripts/, temp files)
- [ ] Update documentation
- [ ] Final commit

---

## Success Metrics

| Metric | Baseline | Target |
|--------|----------|--------|
| Accounts migrated (web) | 0 | 1000 |
| Accounts authorized (fragment) | 0 | 1000 |
| Zombie processes after batch | Unknown | 0 |
| Peak RAM during batch | Unknown | < 12GB (75%) |
| Tests passing | 269 | 275+ |
| System crashes | Expected | 0 |

---

## Источники анализа

- 6 parallel audit agents (resource leaks, dead code, security, architecture, fragment/GUI, test gaps)
- 9 expert analyses (Werner Vogels, Brendan Gregg, Sam Newman, Martin Fowler, Don Norman, Troy Hunt, Kent C. Dodds, Gene Kim, Kelsey Hightower)
- Claude Code subagents docs: https://code.claude.com/docs/en/sub-agents
