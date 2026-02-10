# Full Audit & Production Plan — 1000 аккаунтов

**Дата:** 2026-02-10
**Аудиторы:** 4 параллельных агента (code-explorer, security-auditor, code-reviewer, expert-architect)
**Scope:** 9565 строк src/, 269 тестов, 15 модулей

---

## Резюме

Проект на **~85% готов** к продакшену. SQL injection / command injection — нет. DB tracking, circuit breaker, retry, shared BrowserManager, PID-based kill — работают. Но есть **4 бага** которые гарантированно сломают работу на 1000 аккаунтах, **3 архитектурных проблемы** и **6 security-находок** требующих внимания.

---

## PHASE 1: Блокеры масштабирования

### BUG-1: ~~proxy_relay leak при retry browser launch~~ [FALSE POSITIVE]
**Статус:** Верифицировано — НЕ баг. FIX-A outer except (строка 595) уже чистит relay на ЛЮБОМ исключении. Relay намеренно жив между первой попыткой и retry — retry использует тот же localhost relay.

### BUG-2: completed_count считает retries [CRITICAL → FIXED]
**Файл:** `src/worker_pool.py:292`
**Суть:** `self._completed_count += 1` выполнялся для КАЖДОГО результата, включая retry.
**Фикс:** Перемещён внутрь `if not is_retry:`. Progress callback тоже не вызывается для retries.

### BUG-3: ~~double-click race в _migrate_selected~~ [FALSE POSITIVE]
**Статус:** Верифицировано — НЕ баг. DearPyGui callbacks однопоточные (main thread). Кнопки disabled синхронно на строке 1076, до `_run_async`. Второй клик физически невозможен.

### BUG-4: Fragment auth asyncio.Event loop conflict [MEDIUM — DEFERRED]
**Файл:** `src/fragment_auth.py:390`
**Суть:** Потенциальный конфликт event loop. Низкая вероятность при текущем использовании (worker pool изолирует async контекст). Отложен до реального тестирования Fragment.

---

## PHASE 2: Архитектурная консолидация

### ARCH-1: Три дублирующих batch migration системы
**Проблема:**
| Система | Файл | Используется | Retry | DB tracking | Shared BM |
|---------|------|-------------|-------|-------------|-----------|
| `migrate_accounts_batch()` | telegram_auth.py:1666 | CLI `--all` | нет | нет | нет |
| `ParallelMigrationController` | telegram_auth.py:1820 | CLI `--parallel` | нет | нет | нет |
| `MigrationWorkerPool` | worker_pool.py:82 | GUI | да | да | да |

CLI `--parallel` создаёт НОВЫЙ BrowserManager для каждого worker → LRU eviction не работает → OOM на 50+ акков.

**Фикс:** CLI должен использовать MigrationWorkerPool (тот же pool что GUI). Удалить `migrate_accounts_batch()` и `ParallelMigrationController` (~400 строк dead code).

### ARCH-2: migration_state.py — 321 строка deprecated кода
**Фикс:** Удалить полностью. Все ссылки уже перенесены в database.py.

### ARCH-3: telegram_auth.py — 2038 строк, 6+ ответственностей
**Содержит:** AccountConfig, QR-декодирование, Telethon client, авторизация, cooldown, CircuitBreaker, ParallelMigrationController, batch helpers.
**Фикс (если будет время):** Выделить QR-декодирование в `qr_decode.py`, CircuitBreaker в `circuit_breaker.py`. Минимум: удалить deprecated Controller.

---

## PHASE 3: Security (исправить до передачи клиенту)

### SEC-1: Proxy credentials в plaintext [CRITICAL]
- SQLite таблица `proxies` хранит username/password в plaintext
- `profile_config.json` содержит полную proxy строку с кредами
- pproxy subprocess получает креды через command-line args (видно в Task Manager)
**Фикс минимальный:** Убрать `-v` из pproxy args. Добавить `.gitignore` для `accounts.zip`, `*.txt` proxy файлов, `popup_debug_*.html`.
**Фикс полный:** Передавать proxy через env vars или stdin pipe.

### SEC-2: 2FA пароль в process list [CRITICAL]
**Файл:** `src/cli.py:109` — `--password` видна в `ps aux` / Task Manager
**Фикс:** Использовать только env var `TG_2FA_PASSWORD` или `click.prompt(hide_input=True)`.

### SEC-3: Path traversal в именах аккаунтов [MEDIUM]
**Файл:** `src/browser_manager.py:325`, `src/cli.py:65`
**Суть:** Имя аккаунта напрямую используется в `profiles_dir / name` без санитизации. Если `___config.json` содержит `Name: "../../etc/sensitive"` → path traversal.
**Фикс:** Валидировать имена: отклонять `..`, `/`, `\`.

### SEC-4: ZIP slip в распаковке профилей [MEDIUM]
**Файл:** `src/browser_manager.py:194-196`
**Суть:** `zf.extractall(profile_path)` без проверки путей архива.
**Фикс:** Валидировать member paths перед extraction.

### SEC-5: TOCTOU в find_free_port() [HIGH]
**Файл:** `src/proxy_relay.py:60-66`
**Суть:** Порт освобождается между нахождением и привязкой pproxy. Локальный атакующий может перехватить порт.
**Фикс:** Передавать pre-bound socket или использовать SO_REUSEADDR.

### SEC-6: Debug screenshots не чистятся [HIGH]
**Файл:** `src/telegram_auth.py:1117+`
**Суть:** Скриншоты с QR-кодами, 2FA полями, номерами телефонов сохраняются без cleanup.
**Фикс:** Автоочистка после успешной миграции. Отключить в production.

---

## PHASE 4: Pre-production checklist

### Disk space
1000 профилей × 100-600MB = **100-600GB**. Проверить свободное место ДО запуска.

### RAM budget (16GB)
- 8 workers × 1 Camoufox (~300MB) = 2.4GB
- Python + SQLite + overhead = ~1GB
- 20 hot profiles × 100MB = 2GB
- **Total: ~5.5GB** — укладываемся

### SQLite WAL
На 1000 записей WAL файл может вырасти до 500MB+.
**Фикс:** Периодический `PRAGMA wal_checkpoint(TRUNCATE)` (каждые 100 аккаунтов).

### Fragment.com
CSS-селекторы НЕ проверены на реальном сайте. Тестировать на 1 аккаунте руками ПЕРЕД batch запуском.

---

## PHASE 5: Rollout стратегия

| Day | Batch | Акков | Цель |
|-----|-------|-------|------|
| 1 | Fixes | 0 | Исправить BUG-1, BUG-2, BUG-3 |
| 2 | Smoke | 10 | Проверить RAM, zombies, DB consistency |
| 3 | Canary | 50 | 6 часов мониторинга, проверить web.telegram.org login |
| 4 | Batch 1 | 100 | Масштаб, стабильность |
| 5-7 | Batch 2-5 | 4×200 | Основной объём |
| 8 | Batch 6 | остаток | Завершение web migration |
| 9 | Fragment | 1000 | fragment.com авторизация (после тестирования CSS) |

---

## Что НЕ трогать

- **Рефакторинг telegram_auth.py** — работает, не ломать
- **Prometheus/Grafana** — overkill для desktop app
- **SQLite connection pool** — WAL + busy_timeout=30s достаточно для 8 workers
- **GoLogin export** — отдельная задача, после миграции
- **Тесты** — 269 штук, все зелёные, не трогать пока не сломаем

---

## Порядок исправлений (зависимости)

```
BUG-2: completed_count        [5 строк, 0 зависимостей]  ← ПЕРВЫМ
BUG-1: proxy_relay leak       [15 строк, 0 зависимостей]
BUG-3: double-click race      [3 строки, 0 зависимостей]
SEC-1: .gitignore update      [3 строки, 0 зависимостей]
ARCH-2: удалить migration_state.py [0 строк новых, -321 строк]
──────────────────────────────────────────────────────────────
pytest → всё зелёное
──────────────────────────────────────────────────────────────
Smoke test 10 акков
Canary 50 акков
Production rollout
```

---

## Файлы изменений

| Файл | Что | Строк |
|------|-----|-------|
| `src/worker_pool.py` | BUG-2: move completed_count | ~5 |
| `src/browser_manager.py` | BUG-1: proxy_relay cleanup on retry | ~15 |
| `src/gui/app.py` | BUG-3: early button disable | ~3 |
| `.gitignore` | SEC-1: accounts.zip, *.txt, debug html | ~3 |
| `src/migration_state.py` | ARCH-2: DELETE | -321 |
| `tests/test_migration_state.py` | ARCH-2: DELETE | ~-120 |

---

## Положительные выводы аудита

Что уже хорошо:
- **SQL injection** — нет. Все запросы параметризованы, whitelist полей для UPDATE
- **Command injection** — нет. Все subprocess через list args, нет `shell=True`
- **PID-based kill** — правильно, не `taskkill /IM`
- **WAL + busy_timeout** — SQLite concurrency решена
- **Circuit breaker** — cascade failure protection работает
- **Session isolation** — 1 аккаунт = 1 профиль = 1 прокси
- **WebRTC blocking** — `block_webrtc: True` в Camoufox
- **Anti-ban cooldowns** — 60-120s рандомизированные
- **Crash recovery** — `reset_interrupted_migrations()` на старте
- **Phone/proxy masking** — в логах маскируются
