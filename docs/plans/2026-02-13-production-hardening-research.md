# Production Hardening Research Plan
**Date**: 2026-02-13
**Goal**: Ensure 1000 accounts can be migrated (web + fragment) without re-doing work due to bugs

## Context

### What we KNOW works (real data, not mocks):
- Web QR migration: 9/9 live accounts OK
- Fragment OAuth: 6/7 individual runs OK (1 timeout — rate limit)
- Fragment batch --all: 1/14 OK (13 FAIL — fragment.com unreachable via VPN on dev machine)
- 326 unit tests pass
- EXE built (670MB)

### What we DON'T know (never tested in reality):
1. Parallel web migration with real accounts (only sequential tested)
2. Fragment batch with real working proxies (dev machine VPN blocks fragment.com)
3. EXE on clean Windows machine (never tested)
4. 100+ accounts in one batch (max tested: 14)
5. Recovery after mid-batch crash (--resume flag never tested with real data)
6. Worker pool circuit breaker under real load
7. Profile lifecycle (LRU eviction) with 100+ hot profiles
8. Resource monitor adaptive concurrency with real browsers
9. Proxy rotation/replacement mid-batch
10. Database operation_log at scale (10k row rotation)

### Real bugs found in production logs:
- RuntimeWarning `Page.wait_for_event` (FIXED: e54d06c)
- `IncompleteReadError: 0 bytes read on 8 expected` (line 316 of batch log — NOT FIXED)
- `LeakWarning: geoip=True recommended` (line 348 — cosmetic but noisy)
- `Automatic reconnection failed 0 time(s)` — Telethon MTProto sender (transient?)

## Research Dimensions (7 parallel agents)

### Agent 1: Error Recovery & Retry Logic
**Files**: src/worker_pool.py, src/telegram_auth.py, src/fragment_auth.py, src/cli.py
**Questions**:
- What happens when a worker crashes mid-migration? Is the account left in limbo?
- Does --resume correctly pick up where batch left off?
- Does --retry-failed work for web migration? What about fragment?
- What errors are retryable vs terminal? Is the classification correct?
- What happens if Telethon throws FloodWaitError mid-batch? Do ALL workers stop?
- After circuit breaker opens, do already-completed accounts get re-processed?
- Is there a transaction-like mechanism — either the account is fully migrated or rolled back?
- What if browser crashes after QR accepted but before profile saved?

### Agent 2: Resource Leaks & Zombie Processes
**Files**: src/browser_manager.py, src/proxy_relay.py, src/telegram_auth.py, src/gui/app.py
**Questions**:
- After 100 sequential account migrations, are all browsers properly closed?
- Are all pproxy relay processes killed after each account?
- What happens if the process is killed with Ctrl+C mid-migration?
- Does atexit handler actually work in EXE mode?
- Are Telethon connections properly disconnected after each account?
- Can zombie browsers accumulate over a 1000-account run?
- ProfileLifecycleManager: does LRU eviction work correctly under pressure?
- What happens when disk fills up from 1000 browser profiles?

### Agent 3: Fragment Auth Robustness
**Files**: src/fragment_auth.py, src/cli.py (fragment command), src/worker_pool.py
**Questions**:
- Fragment has no --retry-failed. What's the client workflow for failed accounts?
- If fragment.com returns 500 or Cloudflare block, what happens?
- If OAuth popup shows captcha, what happens?
- Code from 777000 arrives but popup is already closed — what happens?
- What if phone number format doesn't match (country code differences)?
- Is there timeout handling for every async operation in the flow?
- What happens if stel_ssid cookie is set but login didn't actually complete?
- How does "already authorized" detection work? Can it give false positives?

### Agent 4: Database Integrity & State Management
**Files**: src/database.py, src/cli.py, src/worker_pool.py
**Questions**:
- If process crashes mid-write to SQLite, is data consistent (WAL journal)?
- Can two workers update the same account simultaneously?
- Is there a race between batch_pause and account status updates?
- What happens to `batches` table if batch is never finished (crash)?
- Does get_batch_pending correctly exclude completed AND failed accounts?
- If account is marked migrated in DB but profile is corrupted, what happens?
- operation_log rotation: does it lose important error data at scale?
- Can the DB file grow unbounded? What's estimated size for 1000 accounts?

### Agent 5: Proxy & Network Edge Cases
**Files**: src/proxy_manager.py, src/proxy_relay.py, src/proxy_health.py, src/browser_manager.py
**Questions**:
- What if a proxy dies mid-migration? Is there automatic failover?
- pproxy relay process crash — does it take down the browser too?
- SOCKS5 auth failure — does it retry with a new proxy or fail permanently?
- What if all proxies are dead? Does the batch gracefully stop?
- find_free_port TOCTOU race — how likely under parallel workers?
- proxy_health check passes but proxy fails during actual migration — handled?
- Proxy credential rotation: can proxies be updated without restarting batch?
- What happens when proxy returns slow responses (not timeout, just slow)?

### Agent 6: GUI Reliability Under Load
**Files**: src/gui/app.py, src/gui/controllers.py, src/gui/theme.py
**Questions**:
- Can GUI handle 1000 rows in the table without lag?
- What happens if user clicks "Migrate All" twice fast?
- STOP button during batch — does it gracefully stop all workers?
- Progress updates: do they throttle correctly to prevent GUI freeze?
- Error display: do long error messages overflow/crash the table?
- Fragment status column: is it updated in real-time during batch?
- Memory usage of GUI deque(2000) with 1000-account batch logging?
- What if GUI loop crashes — are background workers orphaned?

### Agent 7: EXE & Deployment Edge Cases
**Files**: build_exe.py, TGWebAuth.spec, src/paths.py, main.py, src/proxy_relay.py
**Questions**:
- Does in-process pproxy work correctly when frozen?
- Are all hidden imports actually needed? Any missing?
- What happens on first run (empty DB, no accounts, no profiles)?
- Does the EXE handle Windows Defender interference?
- Long file paths with Cyrillic account names in profiles/ — handled?
- File permissions: can EXE write to its own directory?
- What if Camoufox binary is missing from the bundle?
- Auto-updater: is there a way for client to update without re-downloading 670MB?

## Deliverables
Each agent produces:
1. **Findings list** — every issue found with severity (P0/P1/P2/P3)
2. **Code evidence** — exact file:line references
3. **Fix recommendation** — how to fix each issue
4. **Not-a-bug confirmations** — things that look wrong but are actually OK (with proof)
