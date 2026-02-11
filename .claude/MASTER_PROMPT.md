# TG WEB AUTH - MASTER PROMPT

> **READ THIS FIRST.** If you lost context or this is a new session, read this entire file.
> Then read: `docs/ACTION_PLAN_2026-02-08.md` for current tasks.

---

## 1. PROJECT CONTEXT

**Goal:** Migrate 1000 Telegram session files (.session SQLite) into browser profiles
for web.telegram.org (QR login) and fragment.com (Telegram Login Widget).

**Hardware:** Ryzen 5600U (6C/12T), 16GB RAM, NVMe SSD, Windows.
**Priority:** Stability > Anti-ban safety > Speed.
**Codebase:** ~10K lines src/, 287 tests, Python 3.11+ async.

### How QR Login Works
```
Telethon (existing session) --AcceptLoginTokenRequest(token)--> Telegram Server
Camoufox browser opens web.telegram.org --> shows QR code --> screenshot --> decode token
Result: browser gets authorized web session, saved as persistent profile
After: SetAuthorizationTTL(365 days) extends session to 1 year
```

### Key Constraints
- Max 5-8 parallel browsers (16GB RAM limit)
- Safe speed: 10-20 QR logins/hour, 60-120s random cooldown per account
- 1 dedicated SOCKS5 proxy per account (NEVER share)
- NEVER use same .session from 2 clients simultaneously (AUTH_KEY_DUPLICATED = session death)
- Web sessions persist for MONTHS (set to 1 year via setAuthorizationTTL)
- Keep-alive: weekly Telethon API call (updates.getState), no browser needed
- Fragment.com auth is SEPARATE from web.telegram.org (different cookies/flow)
- Camoufox has memory leaks - use short sessions only (30-60s), then kill
- opentele does NOT support web sessions - QR login is the ONLY viable method

### File Structure
```
src/telegram_auth.py     # Core QR auth (2038 lines) - main flow
src/fragment_auth.py     # Fragment.com OAuth popup + fragment_account() (689 lines)
src/browser_manager.py   # Camoufox + ProfileLifecycleManager + PID kill (761 lines)
src/worker_pool.py       # Asyncio queue pool, mode web/fragment (622 lines)
src/cli.py               # CLI 9 commands (978 lines)
src/database.py          # SQLite: accounts, proxies, migrations, WAL (907 lines)
src/proxy_manager.py     # Import, health check, auto-replace (442 lines)
src/proxy_relay.py       # SOCKS5->HTTP relay via pproxy (280 lines)
src/proxy_health.py      # Batch TCP check (103 lines)
src/resource_monitor.py  # CPU/RAM monitoring (159 lines)
src/security_check.py    # Fingerprint/WebRTC check (372 lines)
src/migration_state.py   # DEPRECATED JSON state (to be deleted)
src/utils.py             # Proxy parsing helpers (103 lines)
src/logger.py            # Logging setup (83 lines)
src/gui/app.py           # DearPyGui main window (1292 lines, 85% complete)
src/gui/controllers.py   # GUI business logic (278 lines)
src/gui/theme.py         # Hacker-style dark green theme (99 lines)
tests/                   # 287 tests
accounts/                # Source session files (gitignored)
profiles/                # Browser profiles (gitignored)
data/                    # SQLite database (tgwebauth.db)
docs/                    # Plans, research, session notes
```

---

## 2. WHAT'S BUILT AND WORKING

| Feature | Module | Status |
|---------|--------|--------|
| QR Login (single + batch) | telegram_auth.py | Working |
| Multi-decoder QR | telegram_auth.py | Working (jsQR, OpenCV, pyzbar) |
| Camoufox antidetect | browser_manager.py | Working |
| PID-based force-kill browsers | browser_manager.py | Working (psutil, per-PID) |
| Profile hot/cold tiering | browser_manager.py | Working (ProfileLifecycleManager) |
| Shared BrowserManager in pool | worker_pool.py | Working (global LRU) |
| SOCKS5 proxy relay | proxy_relay.py | Working, has process leaks |
| Parallel migration (CLI) | telegram_auth.py | Working (ParallelMigrationController) |
| Worker pool (GUI, web+fragment) | worker_pool.py | Working (MigrationWorkerPool) |
| Fragment.com OAuth | fragment_auth.py | Live verified 1/1 (3388c58), CSS checked via Playwright MCP, ready for canary |
| Fragment batch auth (GUI) | worker_pool.py + gui/app.py | Working (Fragment All button) |
| SQLite state management | database.py | Working (5 tables, WAL + busy_timeout) |
| Proxy import/check/replace | proxy_manager.py, proxy_health.py | Working |
| Auth TTL 365 days | telegram_auth.py | Working |
| Resource monitor | resource_monitor.py | Working |
| Security fingerprint check | security_check.py | Working |
| CLI (9 commands) | cli.py | Working |
| GUI (DearPyGui) | gui/ | 85% complete |

## 3. WHAT'S BROKEN / TODO

| Issue | Severity | Where |
|-------|----------|-------|
| ~~Zombie browsers at close() timeout~~ | ~~P0~~ FIXED | browser_manager.py (PID kill) |
| ~~taskkill /IM kills ALL browsers~~ | ~~P0~~ FIXED | browser_manager.py (per-PID) |
| ~~SQLite "database is locked" parallel~~ | ~~P0~~ FIXED | database.py (WAL + busy_timeout) |
| ~~No shared BrowserManager~~ | ~~P1~~ FIXED | worker_pool.py |
| ~~No batch Fragment auth~~ | ~~P1~~ FIXED | worker_pool.py + gui/app.py |
| ~~GUI shutdown leaves zombies~~ | ~~P1~~ FIXED | gui/app.py |
| ~~GUI progress rebuilds 1000x~~ | ~~P2~~ FIXED | gui/app.py (throttle 3s) |
| ~~proxy_relay process leak on retry~~ | ~~P1~~ FIXED | proxy_relay.py (FIX-B: kill on health check fail) |
| ~~proxy_relay leak on non-timeout exception~~ | ~~P0~~ FIXED | browser_manager.py (FIX-A: outer try/except) |
| ~~proxy_relay broken state on retry~~ | ~~P1~~ FIXED | browser_manager.py (relay recreation on retry) |
| ~~CLI no orphan cleanup on exit~~ | ~~P1~~ FIXED | cli.py (atexit psutil killer + KeyboardInterrupt) |
| ~~task_done() deadlock on exception~~ | ~~P0~~ FIXED | worker_pool.py (FIX-D: finally block) |
| ~~BrowserManager not closed on pool exit~~ | ~~P0~~ FIXED | worker_pool.py (FIX-C: run() try/finally) |
| ~~taskkill /IM kills parallel workers~~ | ~~P0~~ FIXED | browser_manager.py (FIX-E: removed, log only) |
| ~~GUI double-click race condition~~ | ~~P1~~ FIXED | gui/app.py (FIX-F/G: guard + button disable) |
| ~~Fragment CSS selectors unverified~~ | ~~P0~~ VERIFIED | fragment_auth.py (Playwright MCP snapshots + fallback selectors) |
| Worker pool not in CLI | P1 | cli.py (deprioritized — GUI is production path) |
| migration_state.py still imported | P2 dead code | cli.py |
| QR decode len check wrong (FIX-001) | P2 | telegram_auth.py |
| 2FA selector hardcoded (FIX-005) | P2 | telegram_auth.py |
| Duplicate proxy parsing in 4 files | P2 cleanup | various |

---

## 4. TELEGRAM SAFETY RULES (NON-NEGOTIABLE)

```
NEVER use same session from 2 clients simultaneously
NEVER log auth_key, api_hash, passwords, tokens, phone numbers
NEVER share proxy between accounts
ALWAYS use same proxy for Telethon AND browser per account
ALWAYS randomize cooldown (60-120s) between operations
ALWAYS close browser + kill pproxy in finally blocks
ALWAYS set auth TTL to 365 days after successful migration
```

---

## 5. PYTHON ENVIRONMENT

```bash
# Run tests
pytest -v --tb=short

# Install deps
pip install -r requirements.txt

# GUI
python -m src.gui.app

# CLI
python -m src.cli [command]
```

---

## 6. CONTEXT RECOVERY PROTOCOL

If you lost context (auto-compaction, new session, etc.):

```
Step 1: Read this file (.claude/MASTER_PROMPT.md)
Step 2: Read docs/ACTION_PLAN_2026-02-08.md
Step 3: Run: TaskList (check in-progress tasks)
Step 4: Run: git log --oneline -10 (see recent commits)
Step 5: Run: git diff --stat (see uncommitted changes)
Step 6: Run: pytest -v --tb=short (check test status)
Step 7: Resume work from where you left off
```

### Available MCP Tools (USE PROACTIVELY)
These tools are available via MCP servers. **Always check for them after context loss:**
- **Serena** — symbolic code editing: `find_symbol`, `replace_symbol_body`, `search_for_pattern`, `get_symbols_overview`
- **Context7** — library docs: `resolve-library-id` → `query-docs`
- **Playwright MCP** — browser automation: `browser_snapshot`, `browser_click`, `browser_navigate`
- **Tavily** — web search: `tavily_search`, `tavily_extract`, `tavily_research`
- **Filesystem MCP** — file ops: `read_text_file`, `write_file`, `edit_file`, `directory_tree`

---

## 7. PROGRESS TRACKER

Update this section as phases complete:

```
Phase A: Stabilization         [x] DONE (Phase 1 of production plan complete)
  A.1: Resource leaks fix      [x] DONE (FIX-A/B/C/D/E — proxy_relay, browser, pool, task_done)
  A.2: Critical bugs fix       [x] DONE (SQLite WAL, GUI race conditions FIX-F/G)
  A.3: Worker pool in CLI      [ ] NOT STARTED (deprioritized — GUI is production path)
  A.4: Dead code cleanup       [ ] NOT STARTED (after canary)

Phase B: Fragment.com          [x] DONE
  B.1: Verify on real site     [x] DONE (CSS verified via Playwright MCP, live test 1/1 in 3388c58)
  B.2: Batch Fragment via GUI  [x] DONE (Fragment All button, mode=fragment in pool)

Phase C: Production            [ ] READY TO START (Phase 1 blockers resolved)
  C.1: Pre-flight checks       [ ] NOT STARTED
  C.2: Canary (5-10 accs)      [ ] NOT STARTED
  C.3: Production (990 accs)   [ ] NOT STARTED
  C.4: Keep-alive setup        [ ] NOT STARTED

Phase D: GUI Polish            [~] IN PROGRESS
  D.1: Fragment All button     [x] DONE
  D.2: Shutdown kills pool     [x] DONE
  D.3: Progress throttle       [x] DONE
  D.4: Button disable/guard    [x] DONE (FIX-F + FIX-G)
  D.5: Full button testing     [ ] NOT STARTED
```

Previously completed (not tracked here):
- Core QR login, batch migration, CLI, SQLite, worker pool,
  profile lifecycle, proxy management, auth TTL, resource monitor,
  fragment auth rewrite, GUI 85%, 269 tests.

Completed in session 2026-02-09 (early):
- PID-based force-kill zombie browsers (psutil, not taskkill /IM)
- SQLite WAL + busy_timeout=30s
- Shared BrowserManager across worker pool (global LRU)
- Fragment batch auth (mode=fragment in worker_pool + GUI Fragment All)
- GUI shutdown stops active pool
- GUI progress throttle (3s interval)

Completed in session 2026-02-09 (Phase 1 implementation):
- FIX-A: proxy_relay cleanup on ANY exception in browser launch (outer try/except)
- FIX-B: proxy_relay.start() kills subprocess on health check failure
- FIX-C: worker_pool run() try/finally with browser_manager.close_all()
- FIX-D: task_done() moved to finally block (prevents queue deadlock)
- FIX-E: Removed dangerous taskkill /IM fallback (prevents killing parallel workers)
- FIX-F: _migrate_all() + _migrate_selected() active pool guard
- FIX-G: Button tags + _set_batch_buttons_enabled() disable during batch ops
- Code review: fixed double proxy_relay.stop(), added missing guard in _migrate_selected()
- 14 new tests (255 → 269)

Completed in session 2026-02-10:
- Proxy relay recreation on browser launch retry (stop old → new ProxyRelay → fresh port)
- CLI atexit orphan killer (psutil cmdline for pproxy, name for camoufox/firefox)
- CLI KeyboardInterrupt handler for fragment --all
- 2 new tests (269 → 271)
- Audit: found & fixed pproxy cmdline detection bug (python.exe -m pproxy, not "pproxy")
- Smoke test attempt: account 573189220650 has dead session (expected — needs live sessions)

Production audit (5 parallel agents, 50+ findings → 18 fixes):
- Block 1 Anti-ban (7): circuit breaker single-probe, global batch pause, dedup accounts,
  QR stale token non-retryable, queue.join timeout, shared BrowserManager in CLI, cooldown after completion
- Block 2 Resource leaks (6): fragment_single BrowserManager P0, per-row buttons disable,
  log deque(500), incremental table update, async zip I/O, CLI fragment shared BrowserManager
- Block 3 Security (5): proxy creds stripped from configs/logs/errors, assign_proxy check
- 16 new tests (271 → 287), reviewer verdict: SHIP IT

**STOPPED AT:** Phase 2 — Smoke Test. Need accounts with live Telethon sessions.
Next: find/verify 10 accounts with live sessions → GUI "Migrate All" → monitor RAM/zombies.

Last updated: 2026-02-10
