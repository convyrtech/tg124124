# Final QA Plan: Zero-Defect Production Release

> **Status:** Research complete
> **Date:** 2026-02-16
> **Goal:** Найти и устранить ВСЕ оставшиеся баги, довести софт до production-ready состояния для 1000 аккаунтов

---

## Table of Contents

1. [Root Cause Analysis](#1-root-cause-analysis)
2. [Revised Phase Pipeline](#2-revised-phase-pipeline)
3. [Tool Stack](#3-tool-stack)
4. [Coverage Gaps to Close](#4-coverage-gaps-to-close)
5. [Scale Testing Strategy](#5-scale-testing-strategy)
6. [PyInstaller EXE Hardening](#6-pyinstaller-exe-hardening)
7. [DB Migration & Backward Compat](#7-db-migration--backward-compat)
8. [Library-Specific Fixes](#8-library-specific-fixes)
9. [Implementation Plan](#9-implementation-plan)
10. [Success Metrics](#10-success-metrics)

---

## 1. Root Cause Analysis

### Why 7 audit rounds kept finding P0/P1 bugs

**Structural deficiency**: каждый аудит и каждый тест рассматривал модули как изолированные стены. 633 mock-использования в 344 тестах означают, что тест-сьют верифицирует поведение каждого модуля *в изоляции*, но НИКТО не верифицировал, что модуль A реально отправляет правильные данные в модуль B.

**Доказательства:**
- DB race (Round 2) — worker_pool вызывает несколько database методов через await points. Unit тесты мокают DB → interleaving невидим
- Absolute paths (Round 5) — cli.py пишет `str(abs_path)`, worker_pool читает и передаёт в `Path()`. Оба работают изолированно. Баг в формате контракта данных
- NULL columns (Round 6) — worker_pool вызывает `db.update_account()`, но НИКОГДА не вызывал с `web_last_verified`. Метод DB работает — его просто не вызывали с нужными полями

**Pattern**: тематические аудиты находят тематические баги. Cross-cutting баги (DB + worker + deployment) систематически пропускаются.

### Never-checked bug categories (highest risk)

| # | Category | Risk | Description |
|---|----------|------|-------------|
| 1 | Concurrent GUI + CLI | P0 | Разные процессы → asyncio.Lock per-process → нет координации на SQLite |
| 2 | Clock/timezone | P1 | `datetime.now()` (local) vs `CURRENT_TIMESTAMP` (UTC) в auth age calculation |
| 3 | Disk full during operation | P1 | Нет обработки OSError ENOSPC при profile save, zip, DB write |
| 4 | Memory growth over 1000 migrations | P1 | _profile_locks, _retry_counts, Telethon event handlers — never cleaned |
| 5 | Unicode account names | P2 | Через C libraries (pproxy, zxing-cpp) — только APP_ROOT проверяется |
| 6 | Data survivability / backup-restore | P2 | DB restored from backup + newer profiles = inconsistent state |
| 7 | Proxy credential rotation mid-batch | P2 | Relay uses cached credentials, no re-auth |
| 8 | SSL/TLS MITM by proxy | P3 | Telegram Web rejects cert, but error not distinguished from timeout |

---

## 2. Revised Phase Pipeline

**Principle**: Dependency-Optimized Pipeline с Incremental Fixes. Каждая фаза КОРМИТ следующую.

### Stage 1: Lint Baseline (autofix, safe, mechanical)
```
ruff check src/ --fix --select E,F,W,I,UP
ruff format src/
```
→ Commit. Убирает шум для всех последующих инструментов.

### Stage 2: Type Safety + Async Lint (manual fixes)
```
ruff check src/ --select E,F,W,I,UP,ASYNC,S,TRY,B,C901,RUF
pyright src/ (basic mode → pyrightconfig.json)
flake8-async src/
```
→ Fix manual findings. Commit.

### Stage 3: Security + Coverage Analysis (data collection, NO fixes)
```
bandit -r src/ -ll -f json > reports/bandit.json
detect-secrets scan --exclude-files 'tests/.*' > .secrets.baseline
pip-audit -r requirements.txt
pytest --cov=src --cov-report=term-missing --cov-report=html
radon cc src/ -a -nc -s > reports/radon.json
vulture src/ --min-confidence=80
```
→ Собрать все отчёты в `reports/`. НЕ фиксить. Это ВХОДНЫЕ ДАННЫЕ для Stage 4.

### Stage 4: AI Agent Team Review (5 параллельных агентов)
Каждый агент получает: coverage report, bandit findings, existing audit docs, clean codebase.

| Dimension | Focus | Agent |
|-----------|-------|-------|
| Data Integrity | DB writes, NULL checks, path format, schema consistency | Opus agent |
| Concurrency | async interleaving, lock coverage, race conditions | Opus agent |
| Error Recovery | exception handling, cleanup paths, partial failure | Opus agent |
| Scale & Performance | memory growth, LRU eviction, lock contention | Opus agent |
| Security & Deployment | credentials, EXE paths, proxy auth, frozen mode | Opus agent |

→ Fix AI findings. Commit.

### Stage 5: Contract Integration Tests (NEW — closes root cause)
Написать `tests/test_contracts.py`:
- `test_after_web_migration_db_has_verified_timestamp` — real DB, mock browser
- `test_session_path_is_relative_after_cli_import` — real DB, real filesystem
- `test_concurrent_workers_dont_corrupt_db` — real DB, 8 async tasks
- `test_migration_cleanup_on_cancel` — real DB, cancel mid-flow
- `test_db_portability_after_app_move` — write on one APP_ROOT, read on another

→ Commit.

### Stage 6: Scale & Stress Tests
- `test_1000_accounts_5_workers` — mock migrate, real DB, real asyncio
- `test_lru_eviction_1000_profiles` — 1000 tiny profiles, max_hot=20
- `test_flood_wait_cascade_5_workers` — all workers hit FloodWait
- `test_memory_baseline_50_iterations` — assert bounded dict growth

→ Commit.

### Stage 7: EXE Hardening + Manual Integration
- Remove UPX from spec
- Add smoke test (--smoke-test flag)
- Rebuild EXE
- Manual GUI testing через Windows MCP screenshots
- Test on client's laptop

### Stage 8: Full Retest + Final Commit
```
ruff check src/
pyright src/
bandit -r src/ -ll
pytest -v
```
→ All green → Final commit → Update CLAUDE.md + memories.

---

## 3. Tool Stack

### Install (new)
```bash
pip install bandit vulture pytest-cov radon pip-audit detect-secrets flake8-async
```

### Already available
- ruff 0.12.7
- pyright (via LSP plugin)

### NOT including (with rationale)
| Tool | Why NOT |
|------|---------|
| hypothesis | 80% I/O code, 20% simple parsing — not worth setup cost |
| mutmut | Hours of runtime for mostly-mocked tests. pytest-cov более эффективен |
| pylint | ruff + pyright covers 95%. Pylint adds noise, 10x slower |
| mypy | Two type checkers = conflicting errors. pyright sufficient |
| tryceratops | ruff TRY rules already reimplemented. Just add "TRY" to select |

### Configuration files to create
- `pyrightconfig.json` — basic mode, exclude tests
- `ruff.toml` — select rules, line-length, target-version
- `.secrets.baseline` — detect-secrets baseline

---

## 4. Coverage Gaps to Close

### Code fixes needed (from expert analysis)

| # | Issue | File:Line | Fix |
|---|-------|-----------|-----|
| 1 | `datetime.now()` without timezone (12 locations) | worker_pool, database, gui | Use `datetime.now(timezone.utc).isoformat()` |
| 2 | `time.time()` in CircuitBreaker | telegram_auth:2221 | Replace with `time.monotonic()` |
| 3 | `_profile_locks` dict never cleaned | browser_manager:408 | Prune after `close_all()` |
| 4 | `_retry_counts` dict grows unbounded | worker_pool:164 | Clear on pool.run() start (already done — verify) |
| 5 | `.zip.tmp` orphans not cleaned | browser_manager:226 | Clean in `ensure_active()` |
| 6 | `_access_order.remove()` is O(n) | browser_manager:329 | Switch to `OrderedDict` if stress tests show bottleneck |
| 7 | `_active_pool` shared across threads without lock | gui/app:85 | Add threading.Lock or use thread-safe pattern |
| 8 | Reads without `_db_lock` | database:286-450 | Document as WAL-safe, add comment |
| 9 | DB start_migration no "already migrating" guard | database:652 | Add `WHERE status != 'migrating'` |
| 10 | FloodWait duration not parsed from error | worker_pool | Extract seconds, use `max(extracted, tripled_cooldown)` |

---

## 5. Scale Testing Strategy

### Synthetic stress tests (in CI, seconds)
```python
# tests/test_stress.py
test_1000_accounts_5_workers       # mock migrate, real DB, verify counts
test_db_lock_contention_under_load # 10 workers, 200 accounts, measure lock time
test_lru_eviction_1000_profiles    # 1000 tiny profiles, max_hot=20
test_flood_wait_cascade_5_workers  # all workers FloodWait simultaneously
test_mixed_failures_at_scale       # 70% success, 20% transient, 10% terminal
test_memory_baseline_50_iterations # assert _profile_locks, _retry_counts bounded
test_batch_pause_at_scale          # 100 accounts, pause every 10
```

### Production observability (add to code)
- DB write latency histogram (instrument `_db_lock` acquire time)
- Worker throughput counter (accounts/minute, logged every 10 min)
- Profile lifecycle stats (evictions/min, hot/cold counts)
- FloodWait tracker (count, duration, affected accounts)
- Periodic RSS memory logging (every 60s during batch)

---

## 6. PyInstaller EXE Hardening

| # | Fix | Priority |
|---|-----|----------|
| 1 | Remove UPX (`upx=False`) — AV false positives | P0 |
| 2 | Add `'cryptg'` to hidden imports | P1 |
| 3 | Add timeout to `wait_closed()` in proxy_relay stop | P1 |
| 4 | Log error if bundled camoufox.exe missing (no silent fallthrough) | P1 |
| 5 | Verify `_internal/playwright/driver/node.exe` in build | P1 |
| 6 | Add `--smoke-test` CLI flag for import verification | P2 |
| 7 | Remove unused `screeninfo` from requirements + hidden imports | P3 |

### EXE Verification Checklist
- [ ] `dist/TGWebAuth/TGWebAuth.exe` exists
- [ ] `dist/TGWebAuth/camoufox/camoufox.exe` exists
- [ ] `dist/TGWebAuth/_internal/playwright/driver/node.exe` exists
- [ ] Double-click → GUI opens, no AV block
- [ ] Path with spaces works: `C:\My Apps\TGWebAuth\TGWebAuth.exe`
- [ ] Cyrillic path: `D:\Приложения\TGWebAuth\` (warning, no crash)
- [ ] Close app → no zombie processes
- [ ] 3 parallel workers with SOCKS5 → all relays start/stop cleanly

---

## 7. DB Migration & Backward Compat

### Eager migration on startup (PRAGMA user_version)

```python
# In Database.initialize(), after ALTER TABLE block:
CURRENT_SCHEMA_VERSION = 2

cursor = conn.execute("PRAGMA user_version")
version = cursor.fetchone()[0]

if version < 2:
    # Convert absolute paths to relative
    rows = conn.execute("SELECT id, session_path FROM accounts").fetchall()
    for row in rows:
        old_path = row[1]
        if old_path and Path(old_path).is_absolute():
            try:
                new_path = str(Path(old_path).relative_to(APP_ROOT))
                conn.execute("UPDATE accounts SET session_path=? WHERE id=?",
                            (new_path, row[0]))
            except ValueError:
                # Path from different machine — heuristic recovery
                parts = Path(old_path).parts
                try:
                    idx = parts.index("accounts")
                    relative = str(Path(*parts[idx:]))
                    if (APP_ROOT / relative).exists():
                        conn.execute("UPDATE accounts SET session_path=? WHERE id=?",
                                    (relative, row[0]))
                    else:
                        conn.execute(
                            "UPDATE accounts SET status='error', error_message=? WHERE id=?",
                            (f"Session not found (moved from {old_path})", row[0]))
                except ValueError:
                    pass  # Can't recover, leave as-is

    # Also migrate migrations.profile_path
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    conn.commit()
```

### proxy_manager.py:150 comparison bug
After migration all paths are relative → `a.session_path == session_path_str` works correctly.

---

## 8. Library-Specific Fixes

### Telethon
| Fix | File | Details |
|-----|------|---------|
| `client.disconnect()` timeout | telegram_auth.py:1997 | `asyncio.wait_for(client.disconnect(), timeout=5)` |
| `except BaseException` in finally | telegram_auth.py:1999 | Match fragment_auth pattern |
| Add `auth_restart` to NON_RETRYABLE | worker_pool.py:695 | Prevent retry with stale token |

### Playwright/Camoufox
| Fix | File | Details |
|-----|------|---------|
| Atomic storage_state write | browser_manager.py:854 | Write to .tmp, `os.replace()` |
| Log missing camoufox binary | browser_manager.py:466 | `logger.error()` instead of silent |

### pproxy
| Fix | File | Details |
|-----|------|---------|
| `wait_closed()` timeout | proxy_relay.py:247 | `asyncio.wait_for(..., timeout=5)` |

---

## 9. Implementation Plan

### Phase A: Setup (30 min)
- [ ] Install tools: `pip install bandit vulture pytest-cov radon pip-audit detect-secrets flake8-async`
- [ ] Create `pyrightconfig.json` (basic mode)
- [ ] Create `ruff.toml` (selected rules)

### Phase B: Automated Scans (Stage 1-3) (2-3 hours)
- [ ] Run ruff autofix + format → commit
- [ ] Run ruff manual + pyright + flake8-async → fix → commit
- [ ] Run bandit, detect-secrets, pip-audit, pytest-cov, radon, vulture → save reports

### Phase C: AI Team Review (Stage 4) (1-2 hours)
- [ ] Launch 5 parallel Opus agents with coverage data + security reports
- [ ] Triage findings → fix → commit

### Phase D: Code Fixes (Stage 4 results + Sections 4, 7, 8) (3-4 hours)
- [ ] Fix all 10 coverage gap items (Section 4)
- [ ] Implement DB migration with PRAGMA user_version (Section 7)
- [ ] Apply library-specific fixes (Section 8)
- [ ] Fix EXE issues: UPX, cryptg, wait_closed timeout, camoufox log (Section 6)
- [ ] Commit

### Phase E: New Tests (Stage 5-6) (2-3 hours)
- [ ] Write contract integration tests (test_contracts.py)
- [ ] Write scale/stress tests (test_stress.py)
- [ ] Write frozen-mode tests (test_paths_frozen.py)
- [ ] Run all tests → commit

### Phase F: EXE + Manual Testing (Stage 7) (1-2 hours)
- [ ] Rebuild EXE (upx=False)
- [ ] Smoke test on dev machine
- [ ] Upload to gofile for client testing
- [ ] GUI testing via Windows MCP

### Phase G: Final Retest (Stage 8) (30 min)
- [ ] All automated tools green
- [ ] All tests pass
- [ ] Update CLAUDE.md, session-state, audit-results
- [ ] Final commit

**Total estimated effort: 10-15 hours**

---

## 10. Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Tests passing | 354 | 400+ |
| Code coverage (lines) | Unknown | >70% for src/ |
| Ruff violations | Unknown | 0 (with selected rules) |
| Pyright errors (basic) | Unknown | 0 |
| Bandit high/medium | Unknown | 0 high, <5 medium (documented) |
| Dead code (vulture) | Unknown | 0 (>80% confidence) |
| Known CVEs (pip-audit) | Unknown | 0 critical, 0 high |
| DB schema version | None | PRAGMA user_version = 2 |
| Contract integration tests | 0 | 10+ |
| Stress tests | 0 | 7+ |
| EXE AV triggers | UPX=yes | UPX=no, 0 triggers |

---

## Key Decisions Summary

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Phase ordering | Dependency-optimized (not linear) | Each phase feeds the next |
| Fix timing | Incremental at 3 boundaries | Avoid massive fix batch |
| Type checker | pyright basic (not strict) | Strict floods with 500+ errors from untyped 3rd party |
| Ruff rules | Selected set (not ALL) | ALL = contradictory rules, thousands of noise |
| Mutation testing | Skip | Hours of runtime, mostly mocked tests |
| Property-based | Skip | 80% I/O code, not worth setup |
| pylint | Skip | ruff + pyright = 95% coverage |
| DB migration | Eager + heuristic recovery | Client portability is stated goal |
| UPX | Remove | AV false positives at 706MB bundle |
| Root cause fix | Contract integration tests | Tests the SEAMS, not the modules |
