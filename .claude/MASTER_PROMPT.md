# TG WEB AUTH - MASTER PROMPT

> **READ THIS FIRST.** If you lost context or this is a new session, read this entire file.
> Then read: `docs/plans/2026-02-09-production-1000-design.md` for current tasks.

---

## 1. PROJECT CONTEXT

**Goal:** Migrate 1000 Telegram session files (.session SQLite) into browser profiles
for web.telegram.org (QR login) and fragment.com (Telegram Login Widget).

**Hardware:** Ryzen 5600U (6C/12T), 16GB RAM, NVMe SSD, Windows.
**Priority:** Stability > Anti-ban safety > Speed.
**Codebase:** ~11K lines src/, 326 tests, Python 3.11+ async.

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
src/telegram_auth.py     # Core QR auth (2454 lines) - main flow
src/fragment_auth.py     # Fragment.com OAuth popup + fragment_account() (742 lines)
src/browser_manager.py   # Camoufox + ProfileLifecycleManager + PID kill (849 lines)
src/worker_pool.py       # Asyncio queue pool, mode web/fragment (746 lines)
src/cli.py               # CLI 9 commands (1291 lines)
src/database.py          # SQLite: accounts, proxies, migrations, WAL (1073 lines)
src/proxy_manager.py     # Import, health check, auto-replace (438 lines)
src/proxy_relay.py       # SOCKS5->HTTP relay via pproxy (329 lines)
src/proxy_health.py      # Batch TCP check (244 lines)
src/resource_monitor.py  # CPU/RAM monitoring (162 lines)
src/security_check.py    # Fingerprint/WebRTC check (380 lines)
src/utils.py             # Proxy parsing helpers (103 lines)
src/logger.py            # Logging setup (115 lines)
src/exception_handler.py # Global crash hook (86 lines)
src/paths.py             # Centralized path resolution (21 lines)
src/gui/app.py           # DearPyGui main window (1720 lines, 90% complete)
src/gui/controllers.py   # GUI business logic (266 lines)
src/gui/theme.py         # Hacker-style dark green theme (99 lines)
tests/                   # 326 tests
accounts/                # Source session files (gitignored)
profiles/                # Browser profiles (gitignored)
data/                    # SQLite database (tgwebauth.db)
docs/plans/              # Active plans (3 files)
```

---

## 2. WHAT'S BUILT AND WORKING

| Feature | Module | Status |
|---------|--------|--------|
| QR Login (single + batch) | telegram_auth.py | Working |
| Multi-decoder QR | telegram_auth.py | Working (zxing-cpp, OpenCV, pyzbar) |
| Camoufox antidetect | browser_manager.py | Working |
| PID-based force-kill browsers | browser_manager.py | Working (psutil, per-PID) |
| Profile hot/cold tiering | browser_manager.py | Working (ProfileLifecycleManager) |
| Shared BrowserManager in pool | worker_pool.py | Working (global LRU) |
| SOCKS5 proxy relay | proxy_relay.py | Working (process leaks fixed) |
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
| GUI (DearPyGui) | gui/ | 90% complete |
| PyInstaller EXE | build_exe.py, TGWebAuth.spec | Working (one-folder dist) |

## 3. REMAINING TODO

| Issue | Severity | Where |
|-------|----------|-------|
| 2FA selector hardcoded (FIX-005) | P2 | telegram_auth.py |
| psutil.cpu_percent blocks event loop 100ms | P2 | resource_monitor.py |
| operation_log grows without rotation | P2 | database.py |
| Worker pool not in CLI | P1 | cli.py (deprioritized — GUI is production path) |
| Fragment canary (10 accounts) | P1 | Ready, needs live sessions |
| Production smoke test | P1 | Needs accounts with live Telethon sessions |

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
Step 2: Read docs/plans/2026-02-09-production-1000-design.md
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

```
Phase A: Stabilization         [x] DONE
Phase B: Fragment.com          [x] DONE (live verified 1/1, commit 3388c58)
Phase C: Production            [ ] READY TO START
  C.1: Pre-flight checks       [ ] NOT STARTED
  C.2: Canary (5-10 accs)      [ ] NOT STARTED (needs live sessions)
  C.3: Production (990 accs)   [ ] NOT STARTED
  C.4: Keep-alive setup        [ ] NOT STARTED
Phase D: GUI Polish            [~] 90% DONE
  D.5: Full button testing     [ ] NOT STARTED
Phase E: Packaging             [x] DONE (PyInstaller EXE, one-folder dist)
Phase F: Cleanup               [x] DONE (2026-02-12)
  F.1: Dead code removal       [x] DONE (migration_state.py deleted)
  F.2: Docs cleanup            [x] DONE (12 outdated docs removed)
  F.3: Junk files cleanup      [x] DONE (screenshots, node_modules, temp files)
  F.4: .gitignore update       [x] DONE (.serena/, snapshots, --db-path)
```

**STOPPED AT:** Phase C — Production Smoke Test. Need accounts with live Telethon sessions.
Next: find/verify 10 accounts with live sessions → GUI "Migrate All" → monitor RAM/zombies.

Last updated: 2026-02-12
