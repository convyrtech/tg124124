# TG Web Auth - Action Plan v2

**Date:** 2026-02-06 (updated with research findings)
**Goal:** Production-ready migration of 1000 Telegram sessions
**Based on:** docs/research_project_audit_2026-02-06.md + migration speed/safety research

---

## Critical Research Corrections

| Previous Assumption | Actual Finding |
|---------------------|----------------|
| Web sessions expire in 6 hours | **Months** (default auto-terminate: 6 months, configurable to 1 year) |
| opentele can speed up migration | **No** - only handles Telethon/TDesktop, NOT web.telegram.org |
| 45s cooldown is safe | **Risky** (80/hour). Safe: 60-120s random jitter = 30-50/hour |
| Need 24/7 keep-alive daemon | **No** - weekly Telethon API call is sufficient |
| GUI needs to be built from scratch | **Already 80% built** in src/gui/ (DearPyGui) |
| 40 QR logins/hour is the limit | **~5 new logins/day/account** (official Telegram docs) |

### Session Lifetime Facts
- Authorization (auth_key) is essentially **permanent** until manually terminated
- Auto-terminate setting: configurable 1 week to 1 year (default 6 months)
- Set `account.setAuthorizationTTL(365)` for all migrated accounts
- Keep-alive: weekly `updates.getState` call via Telethon (no browser needed)
- WebSocket connection drops on idle but **reconnects automatically** on page load

### Safe Migration Speed
- **Conservative (recommended):** 10-20 accounts/hour, 60-120s random cooldown
- **With batch pauses:** 10 accounts -> 5-10 min break -> next 10
- **Realistic timeline:** 3-5 days for 1000 accounts
- **MANDATORY:** unique proxy per account (already have webshare dedicated)

---

## Overview

```
Phase 0: GUI Polish & Critical Fixes  [~3-4 days]  -> GUI works, code doesn't crash
Phase 1: Scale Architecture           [~3-4 days]  -> 1000 accounts work
Phase 2: Fragment.com                  [~2-3 days]  -> Fragment auth works
Phase 3: Canary + Production Run       [~3-5 days]  -> Actual migration
                                       ──────────
                                       ~11-16 days total
```

---

## Phase 0: GUI Polish & Critical Fixes (BLOCKERS)

### Task 0.0: GUI Polish (PRIORITY)
**Files:** `src/gui/app.py`, `src/gui/controllers.py`, `src/gui/theme.py`
**Priority:** P0 | **Effort:** Small | **Iterations:** 30

GUI already exists (DearPyGui, 80% complete). Polish:
- [ ] Test launch: `python -m src.gui.app`
- [ ] Fix any startup crashes
- [ ] Complete "Migrate Selected" batch functionality
- [ ] Add proxy display in accounts table
- [ ] Add progress bar for batch operations
- [ ] Add migration speed settings (cooldown slider: 60-120s)
- [ ] Add canary mode toggle (first 5 accounts only)
- [ ] Verify Fragment authorization button works
- [ ] Test with real account data
- [ ] Run pytest - all pass

### Task 0.1: Fix Resource Leaks
**Files:** `browser_manager.py`, `proxy_relay.py`
**Priority:** P0 | **Effort:** Medium | **Iterations:** 50

- [ ] Add timeout to all browser launch operations (max 60s)
- [ ] Fix proxy_relay not closed on launch() TimeoutError
- [ ] Fix _active_browsers dict leak on re-initialization
- [ ] Fix pproxy process leak when returncode check races
- [ ] Fix stop() deadlock when process ignores SIGTERM
- [ ] Add process PID tracking for all child processes
- [ ] Add force-kill for lingering processes in close()
- [ ] Add shutdown handler that kills ALL child processes on exit
- [ ] Write tests for: timeout, crash, cleanup scenarios
- [ ] Run pytest - all pass

### Task 0.2: Fix FIX-001..007
**Files:** `telegram_auth.py`, `browser_manager.py`, `migration_state.py`
**Priority:** P0 | **Effort:** Medium | **Iterations:** 40

- [ ] FIX-001: QR decode grey zone (len check wrong)
- [ ] FIX-002: SQLite "database is locked" in parallel
- [ ] FIX-003: Lock files remain after browser crash
- [ ] FIX-004: Race condition in JSON state writes
- [ ] FIX-005: 2FA selector hardcoded, visibility not checked
- [ ] FIX-006: Telethon connect() hangs for 180s (add timeout)
- [ ] FIX-007: Browser launch hangs without timeout
- [ ] Write tests for each fix
- [ ] Run pytest - all pass

### Task 0.3: Add Missing Timeouts & Safe Limits
**Files:** `telegram_auth.py`, `cli.py`
**Priority:** P0 | **Effort:** Small | **Iterations:** 20

- [ ] Change cooldown from 45s to **60-120s with random jitter** (`random.uniform(60, 120)`)
- [ ] Add batch pauses: after every 10 accounts, pause 5-10 minutes
- [ ] Add max_concurrent limit (max 8 browsers)
- [ ] Add timeout to all page.goto() calls (30s)
- [ ] Add timeout to 2FA password wait (max 60s)
- [ ] Add timeout to Telethon connect (max 30s)
- [ ] Remove old "max 40 QR/hour" - use per-account rate limiting instead
- [ ] Run pytest - all pass

---

## Phase 1: Scale Architecture

### Task 1.1: Worker Pool (Replace ParallelMigrationController)
**Files:** `telegram_auth.py` (new class), `src/gui/controllers.py`
**Priority:** P0 | **Effort:** Medium | **Iterations:** 80

- [ ] Design worker pool with asyncio.Queue
- [ ] Implement MigrationWorkerPool class
  - Bounded queue (maxsize = num_workers * 2)
  - N workers (5-8) pulling from queue
  - Built-in retry (max 2 per account)
  - Graceful shutdown (finish current, cleanup)
  - Resource monitor integration (adaptive worker count)
  - Per-account cooldown: random.uniform(60, 120)
  - Batch pause: 5-10 min every 10 accounts
  - FLOOD_WAIT detection with exponential backoff
- [ ] Add progress reporting callback (for GUI integration)
- [ ] Integrate with GUI batch migration button
- [ ] Integrate with CLI
- [ ] Write tests: concurrency, retry, shutdown, rate limit
- [ ] Test with 20+ mock accounts
- [ ] Run pytest - all pass

### Task 1.2: Consolidate State to SQLite
**Files:** `database.py`, `migration_state.py`
**Priority:** P1 | **Effort:** Medium | **Iterations:** 40

Note: Two databases exist (tg_web_auth.db + tgwebauth.db). Consolidate to ONE.
- [ ] Add missing columns to accounts table (fragment_status, web_last_verified, auth_ttl_days)
- [ ] Add profile_storage table
- [ ] Add operation_log table
- [ ] Migrate MigrationState functionality into Database class
- [ ] Update all callers to use Database instead of MigrationState
- [ ] Consolidate two databases into one (tgwebauth.db)
- [ ] Deprecate migration_state.py
- [ ] Run pytest - all pass

### Task 1.3: Profile Compression Lifecycle
**Files:** `browser_manager.py` (new class ProfileLifecycleManager)
**Priority:** P2 | **Effort:** Medium | **Iterations:** 30

- [ ] Implement ProfileLifecycleManager
  - Tier 1 (Hot): decompressed, recently used
  - Tier 2 (Cold): compressed zip on disk
  - LRU eviction (max 20 hot profiles)
  - ensure_active() - decompress on demand
  - hibernate() - compress and remove active
- [ ] Integrate with BrowserManager.launch()
- [ ] Write tests
- [ ] Run pytest - all pass

### Task 1.4: Proxy Health Check
**Files:** `proxy_relay.py` or `src/gui/controllers.py` (already has check_proxy_connection)
**Priority:** P1 | **Effort:** Small | **Iterations:** 20

Note: Basic TCP proxy check already exists in controllers.py. Extend it.
- [ ] Batch check 1000 proxies in ~20-30 seconds (50 concurrent)
- [ ] Mark dead proxies in database
- [ ] Run before each batch migration (GUI button exists)
- [ ] Add to CLI: `python -m src.cli check-proxies`
- [ ] Write tests
- [ ] Run pytest - all pass

### Task 1.5: Set Authorization TTL (NEW)
**Files:** `telegram_auth.py`
**Priority:** P1 | **Effort:** Small | **Iterations:** 10

- [ ] After successful QR login, call `account.setAuthorizationTTL(365)` via Telethon
- [ ] This maximizes web session lifetime to 1 year
- [ ] Add to migration flow (after browser auth confirmed)
- [ ] Write test
- [ ] Run pytest - all pass

---

## Phase 2: Fragment.com

### Task 2.1: Verify & Fix Fragment Auth
**Files:** `fragment_auth.py`
**Priority:** P0 (for Fragment) | **Effort:** Medium | **Iterations:** 60

- [ ] Use Playwright MCP to open real fragment.com
- [ ] Verify ALL CSS selectors (screenshot each element)
- [ ] Fix 11 critical bugs (see audit doc)
- [ ] Add retry logic with exponential backoff
- [ ] Add Fragment batch auth with rate limiting
- [ ] Integrate with GUI Fragment button
- [ ] Write comprehensive tests
- [ ] Run pytest - all pass

### Task 2.2: Fragment GUI Integration
**Files:** `src/gui/app.py`
**Priority:** P1 | **Effort:** Small | **Iterations:** 15

- [ ] Fragment button already exists in GUI - verify it works
- [ ] Add batch Fragment auth with progress
- [ ] Respect FRAGMENT_SAFETY_LIMITS (3 concurrent, 120s cooldown)
- [ ] Update database with fragment_status
- [ ] Run pytest - all pass

---

## Phase 3: Canary + Production Migration

### Task 3.0: Pre-flight Checks (NEW - MANDATORY before production)
**Priority:** P0 | **Effort:** Small | **Iterations:** 15

- [ ] Verify all 1000 Telethon sessions are alive (Telethon get_me() batch check)
- [ ] Verify all 1000 proxies are alive (TCP check)
- [ ] Verify proxy-account mapping is 1:1 (no sharing)
- [ ] Report: X alive sessions, Y alive proxies, Z ready for migration
- [ ] Add pre-flight check button to GUI
- [ ] Run pytest - all pass

### Task 3.1: Canary Migration (5-10 accounts)
**Priority:** P0 | **Effort:** Small | **Iterations:** 15

- [ ] Select 5-10 "expendable" accounts for canary
- [ ] Run full migration with monitoring
- [ ] Verify: browser profile works, session stays alive 24+ hours
- [ ] Verify: no bans, no FLOOD_WAIT, no AUTH_KEY issues
- [ ] If problems: fix before proceeding
- [ ] Document canary results

### Task 3.2: Production Migration (990 accounts)
**Priority:** P0 | **Effort:** Large

- [ ] Run in batches of 50-100 accounts per day
- [ ] Monitor for FLOOD_WAIT and bans
- [ ] Expected timeline: 3-5 days
- [ ] Daily health check on completed accounts

### Task 3.3: Session Keep-Alive Setup
**Files:** New module or cron job
**Priority:** P1 | **Effort:** Small | **Iterations:** 15

- [ ] Implement weekly batch keep-alive via Telethon `updates.getState`
- [ ] No browser needed - just Telethon API calls
- [ ] Run for all migrated accounts sequentially
- [ ] Add to GUI as scheduled task or manual button
- [ ] Write tests
- [ ] Run pytest - all pass

### Task 3.4: Add Tests for Core Flow
**Files:** `tests/`
**Priority:** P1 | **Effort:** Large | **Iterations:** 40

- [ ] Test TelegramAuth.authorize() with mocked Telethon/Playwright
- [ ] Test BrowserManager.launch() with mocked Camoufox
- [ ] Test FragmentAuth.connect() with mocked browser
- [ ] Test worker pool with mock migration function
- [ ] Target: raise coverage from 3/10 to 7/10
- [ ] Run pytest - all pass

### Task 3.5: Cleanup & Documentation
**Files:** Various
**Priority:** P2 | **Effort:** Small | **Iterations:** 15

- [ ] Remove duplicate proxy parsing (telegram_auth vs utils)
- [ ] Remove experimental scripts/ that don't work (inject_session)
- [ ] Consolidate two databases into one
- [ ] Update CLAUDE.md with new architecture
- [ ] Update README.md with current status
- [ ] Final security scan

---

## Execution Order (Revised)

```
Week 1: Foundation
  Day 1:   Task 0.0 (GUI polish) - PRIORITY
  Day 2:   Task 0.1 (Resource leaks) + Task 0.3 (Timeouts)
  Day 3-4: Task 0.2 (FIX-001..007)

Week 2: Scale
  Day 5:   Task 1.2 (SQLite consolidation) + Task 1.5 (Auth TTL)
  Day 6-7: Task 1.1 (Worker pool + GUI integration)
  Day 8:   Task 1.4 (Proxy health check) + Task 3.0 (Pre-flight)

Week 3: Fragment + Canary
  Day 9-10: Task 2.1 (Fragment fix) + Task 2.2 (Fragment GUI)
  Day 11:   Task 3.1 (Canary migration: 5-10 accounts)

Week 4: Production Run
  Day 12-16: Task 3.2 (Production migration: 990 accounts, 3-5 days)
  Ongoing:   Task 3.3 (Keep-alive setup)
  Parallel:  Task 3.4 (Tests) + Task 3.5 (Cleanup)
```

---

## Success Criteria (Final)

- [ ] GUI launches and all buttons work (`python -m src.gui.app`)
- [ ] 1000 accounts can be queued for migration without OOM
- [ ] 5-8 parallel browsers work without zombie processes
- [ ] All resources cleaned up on normal exit, crash, and timeout
- [ ] SQLite WAL handles concurrent writes without errors
- [ ] Proxy health checked before each migration batch
- [ ] Fragment.com auth works with verified CSS selectors
- [ ] Authorization TTL set to 365 days for all migrated accounts
- [ ] Weekly keep-alive running (Telethon API, no browser)
- [ ] Cooldown: 60-120s random jitter + batch pauses
- [ ] Canary: 5-10 accounts migrated and stable for 24+ hours
- [ ] All tests pass (target: 200+ tests, coverage 7/10)
- [ ] No secrets in logs (auth_key, api_hash, passwords, phones)
- [ ] Security scan passes without critical issues

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Camoufox memory leaks | Short sessions (30-60s), force-kill after close |
| Mass session revocation | 1 proxy/account, 60-120s cooldown, batch pauses, canary first |
| Fragment.com UI changes | Abstract selectors, add fallback detection |
| Disk space (100GB profiles) | Profile compression lifecycle (35-50GB compressed) |
| Proxy failures during batch | Health check before batch, skip dead proxies |
| FLOOD_WAIT errors | Exponential backoff, batch pauses, spread over 3-5 days |
| Account bans | Canary phase first, monitor 24h before scaling |
| Web session expiry | Set TTL to 365 days, weekly Telethon keep-alive |
