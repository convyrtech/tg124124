# Full Production Audit Results
**Date**: 2026-02-13
**Scope**: All source files, 7 dimensions, 1000-account production readiness
**Method**: 4 parallel research agents + manual verification

## Summary

| Severity | Count | Description |
|----------|-------|-------------|
| P1       | 1     | Fragment phone input events not dispatched |
| P2       | 18    | Batch timeout, CancelledError leaks, fragment CLI gaps, proxy, EXE |
| P3       | 16    | Cosmetic, performance, informational |
| **Total**| **35**| (14 verified OK / not-a-bug excluded) |

## TOP 5 MUST-FIX BEFORE 1000-ACCOUNT PRODUCTION

1. **#015 (P1)** — Fragment phone input JS events not dispatched → OAuth may submit blank phone
2. **#001 (P2)** — Batch timeout 3600s too short for 1000 accounts (needs ~2.5h minimum)
3. **#038/#039 (P2)** — CancelledError leak in GUI batch methods (Python 3.11+)
4. **#016/#017 (P2)** — Fragment CLI --all reprocesses authorized + no --retry-failed
5. **#019 (P2→P1)** — Event handler for 777000 code registered AFTER phone submit (race)

---

## DIMENSION 1: ERROR RECOVERY & RETRY

### #001 | P2 | Batch timeout too short
**File**: `src/telegram_auth.py:2307`
**Bug**: `BATCH_TIMEOUT = 3600s` (1 hour). At 90s cooldown with 10 workers, 1000 accounts need ~2.5h.
Batch timeout will cancel 600+ accounts mid-flight.
**Fix**: `BATCH_TIMEOUT = max(7200, len(accounts) * cooldown / max_concurrent * 1.5)` or remove entirely.

### #002 | P2 | migrate_accounts_parallel() creates separate BrowserManagers
**File**: `src/telegram_auth.py:2348-2420`
**Bug**: Each call to `migrate_account()` creates a NEW BrowserManager. 1000 accounts = 1000 independent
managers, defeating LRU profile eviction.
**Fix**: Accept `browser_manager` param or deprecate in favor of `ParallelMigrationController`.

### #004 | P2 | No rollback after QR token accepted
**File**: `src/telegram_auth.py:1734-1741`
**Bug**: After `_accept_token()` succeeds, if `page.reload()` or `_wait_for_auth_complete()` fails,
web session exists on Telegram but browser profile doesn't have cookies. Re-run shows "already authorized"
pre-check but browser may not work.
**Fix**: Always attempt `save_storage_state()` before returning failure after token acceptance.

### #005 | P3 | Sequential CLI creates new BrowserManager per account
**File**: `src/telegram_auth.py:1876-1903`
**Bug**: `migrate_accounts_batch()` creates new `TelegramAuth` per account. LRU eviction doesn't work
cross-account in sequential mode.
**Fix**: Create shared BrowserManager in `migrate_accounts_batch()`.

### #006 | P3 | Batch pause modulo check non-deterministic
**File**: `src/worker_pool.py:704-718`
**Bug**: `completed_total % batch_pause_every == 0` — with concurrent workers, pauses may be skipped
or triggered multiple times.
**Fix**: Use atomic "next pause at" counter.

### #007 | P2 | --resume loses error context on renamed accounts
**File**: `src/cli.py:278-365`
**Bug**: If account directory renamed between crash and resume, accounts orphaned as "migrating" forever.
`reset_interrupted_migrations()` resets to "pending" but loses original error.
**Fix**: Log original error when resetting. Alert about name mismatches.

---

## DIMENSION 2: RESOURCE LEAKS

### #009 | P2 | Sequential CLI: 1000x BrowserManager + ProfileLifecycleManager
**File**: `src/telegram_auth.py:1906-1968`
**Bug**: Each account creates new BrowserManager → new ProfileLifecycleManager → 1000 directory scans.
**Fix**: Create single shared BrowserManager outside the loop.

### #010 | P3 | ProfileLifecycleManager._sync_access_order() 1M stat calls
**File**: `src/browser_manager.py:159-192`
**Bug**: Each init scans all profiles. 1000 managers × 1000 profiles = 1M stat calls.
**Fix**: Share BrowserManager (fixes #009 too).

### #013 | P3 | GUI shutdown kills ALL child processes
**File**: `src/gui/app.py:192-203`
**Bug**: `psutil.Process().children(recursive=True)` kills ALL children, not just ours.
**Fix**: Filter by known process names (camoufox, firefox, pproxy).

---

## DIMENSION 3: FRAGMENT AUTH GAPS

### #015 | P1 | Phone input JS doesn't dispatch events
**File**: `src/fragment_auth.py:387-393`
**Bug**: `element.value = phone` sets DOM property but does NOT dispatch `input`/`change` events.
React-based forms on oauth.telegram.org may submit empty phone.
**Fix**: Add `dispatchEvent(new Event('input', {bubbles: true}))` after setting value.

### #016 | P2 | fragment --all CLI doesn't skip authorized
**File**: `src/cli.py:898-916`
**Bug**: CLI processes ALL account dirs. GUI at app.py:1469 correctly filters `fragment_status != "authorized"`.
With 950/1000 authorized, wastes ~23 hours of cooldown time.
**Fix**: Load DB, filter by fragment_status like GUI does.

### #017 | P2 | No --retry-failed for fragment CLI
**File**: `src/cli.py:848-973`
**Bug**: If fragment fails for 50 accounts, user must re-run --all which reprocesses all 950 authorized.
**Fix**: Add `--retry-failed` flag querying DB for `fragment_status = 'error'`.

### #018 | P2 | No CAPTCHA detection on OAuth popup
**File**: `src/fragment_auth.py:528-685`
**Bug**: If Telegram shows CAPTCHA on oauth.telegram.org, flow silently times out after 60s.
**Fix**: Check for CAPTCHA elements after phone submit, return specific error.

### #019 | P2 | Event handler registered AFTER phone submit (race)
**File**: `src/fragment_auth.py:441 vs 650`
**Bug**: `_confirm_via_telethon()` registers handler (line 441) but is called AFTER `_submit_phone_on_popup`
(line 650). If 777000 code arrives fast, it's missed.
**Fix**: Register handler BEFORE submitting phone, or check recent messages from 777000.

### #020 | P3 | Silent exception swallow after OAuth
**File**: `src/fragment_auth.py:668-674`
**Bug**: `page.reload()` wrapped in bare `except Exception: pass`.
**Fix**: Log at debug level instead of silent pass.

### #021 | P2 | Cookie check only after 30s timeout
**File**: `src/fragment_auth.py:517-526`
**Bug**: stel_ssid cookie checked AFTER JS check times out (30 iterations × 1s). Wastes 30s.
**Fix**: Check cookies inside the loop alongside JS state check.

---

## DIMENSION 4: DATABASE & CONCURRENCY

### #023 | P2 | get_migration_stats() loads all rows into memory
**File**: `src/database.py:716-738`
**Bug**: Fetches ALL account rows to count statuses in Python. With 1000 accounts + large error_messages.
**Fix**: Use SQL GROUP BY (like `get_counts()`).

### #024 | P3 | operation_log rotation O(n log n)
**File**: `src/database.py:984-1028`
**Bug**: DELETE uses subquery with ORDER BY on every rotation.
**Fix**: Use `DELETE WHERE id < (SELECT MAX(id) - 10000)`.

### #025 | P3 | start_batch() 1000 individual queries
**File**: `src/database.py:742-781`
**Bug**: Loop of SELECT + INSERT for each account.
**Fix**: Use `executemany()` or bulk INSERT.

### #026 | P2 | Orphaned batches never auto-closed
**File**: `src/database.py:783-807`
**Bug**: Crashed batch stays open forever. New --all creates new batch without closing old.
**Fix**: Auto-finish open batches on new batch creation.

---

## DIMENSION 5: PROXY & NETWORK

### #029 | P2 | find_free_port TOCTOU race
**File**: `src/proxy_relay.py:61-67`
**Bug**: Port found free, socket closed, pproxy tries to bind — another process can grab it.
**Fix**: Retry find_free_port + start in a loop (up to 3 attempts).

### #030 | P2 | No proxy failover mid-batch
**File**: `src/worker_pool.py:424-444`
**Bug**: Dead proxy → account SKIPPED. No automatic replacement from pool.
**Fix**: Query `db.get_free_proxy()`, assign, retry. Or clear message to run proxy-refresh.

### #033 | P3 | In-process pproxy start exception not caught
**File**: `src/proxy_relay.py:157-163`
**Bug**: If pproxy server raises on start (address in use), _server_handle never set.
**Fix**: Wrap start_server() in try/except.

### #034 | P2 | No pre-flight proxy health check in workers
**File**: `src/proxy_manager.py` / `src/worker_pool.py`
**Bug**: Proxy checked only at import time, not before each migration. Proxy can die between check and use.
**Fix**: Add TCP check before browser launch in worker.

---

## DIMENSION 6: GUI UNDER LOAD

### #035 | P2 | Full table rebuild every 3s with 1000 rows
**File**: `src/gui/app.py:1414-1419`
**Bug**: `_throttled_refresh()` rebuilds entire DearPyGui table. 7000+ widgets destroyed/created.
**Fix**: Ensure `_status_cells` populated before batch, use incremental path.

### #036 | P3 | Log deque(2000) too small for 1000 accounts
**File**: `src/gui/app.py:89`
**Bug**: 3-4 messages/account × 1000 = 3000-4000 messages. Early messages lost.
**Fix**: Increase to 5000 or log to file.

### #037 | P3 | STOP button no visual feedback
**File**: `src/gui/app.py:1407-1412`
**Fix**: Disable button after click, show "Stopping..."

### #038 | P2 | CancelledError leak in _batch_migrate()
**File**: `src/gui/app.py:1448`
**Bug**: `except Exception` doesn't catch CancelledError in Python 3.11+. Pool leaks, buttons stuck.
**Fix**: Change to `except BaseException`.

### #039 | P2 | CancelledError leak in _batch_fragment()
**File**: `src/gui/app.py:1508`
**Bug**: Same as #038 for fragment batch.
**Fix**: Same — `except BaseException`.

### #040 | P3 | Font download hangs on no internet
**File**: `src/gui/app.py:228-238`
**Fix**: Add 5s timeout on urlretrieve.

---

## DIMENSION 7: EXE DEPLOYMENT

### #041 | P2 | No error display if EXE crashes on first launch
**File**: `TGWebAuth.spec:82`
**Bug**: `console=False` → no error output. logs/ may not exist yet.
**Fix**: Create logs/ in runtime_hooks. Add tkinter messagebox in main.py.

### #042 | P2 | Cyrillic paths may break SQLite/pproxy
**File**: `src/paths.py:12-14`
**Bug**: C libraries may fail with non-ASCII APP_ROOT on Windows.
**Fix**: Validate ASCII-safe on startup or warn user.

### #043 | P3 | build_exe copies unnecessary Camoufox cache
**File**: `build_exe.py:59-79`
**Fix**: Exclude `*.log`, `cache2/`, `crashes/` from copytree.

### #045 | P2 | EXE path with spaces may break Camoufox launch
**File**: `src/browser_manager.py:410-416`
**Fix**: Test with `C:\Program Files\TGWebAuth\`.

### #047 | P2 | GUI health check uses wrong Camoufox path in frozen mode
**File**: `src/gui/app.py:1686-1720`
**Bug**: `launch_path()` returns system path, not bundled path.
**Fix**: In frozen mode, check `APP_ROOT / "camoufox" / "camoufox.exe"`.

### #048 | P3 | psutil.cpu_percent returns 0 on first call
**File**: `src/resource_monitor.py:87`
**Fix**: Prime counter in `__init__`.

### #049 | P2 | In-process pproxy can crash event loop
**File**: `src/proxy_relay.py:157-163`
**Bug**: Unhandled pproxy exception in connection handler kills entire event loop.
**Fix**: Wrap in separate task with exception handling.

---

## VERIFIED OK (Not Bugs)

- #003: PoolResult counter mutation — safe in asyncio single-thread
- #008: Fragment event handler cleanup — properly in finally
- #011: ProxyRelayManager stale cache — not used in main flow
- #012: BrowserManager.close_all() iteration — snapshot safe
- #014: Watchdog kill + Playwright exception flow — correct
- #027: Sync sqlite3 in initialize() — works, just inconsistent naming
- #028: update_account() f-string — safe with whitelist
- #031: Proxy relay started before args — caught in except BaseException
- #032: _start_subprocess() in frozen — branch never reached
- #046: pyzbar not bundled — graceful fallback exists
