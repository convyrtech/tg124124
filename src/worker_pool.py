"""
Asyncio Queue-based worker pool for parallel account migration.

Replaces the sequential _batch_migrate() loop in the GUI with a proper
producer-consumer pattern supporting:
- N parallel workers (default 3)
- Per-worker cooldowns (60-120s random)
- Global batch pauses (every 10 accounts, 5-10 min)
- Circuit breaker (5 consecutive failures -> 60s pause)
- Resource monitoring (RAM/CPU checks before each migration)
- Retry on transient errors (up to max_retries)
- FLOOD_WAIT detection (triples cooldown)
- Graceful shutdown via asyncio.Event
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PROFILES_DIR
from typing import Callable, Optional

from .browser_manager import BrowserManager
from .database import AccountRecord, Database
from .fragment_auth import fragment_account
from .resource_monitor import ResourceMonitor
from .telegram_auth import CircuitBreaker, AuthResult, migrate_account

logger = logging.getLogger(__name__)

# Human-readable error messages for non-technical users
_ERROR_MAP = {
    "AuthKeyDuplicated": "Прокси IP уже использовался другим аккаунтом — замените прокси",
    "SessionPasswordNeeded": "Требуется облачный пароль (2FA) для этого аккаунта",
    "PhoneNumberBanned": "Аккаунт заблокирован Telegram",
    "UserDeactivated": "Аккаунт удалён или деактивирован",
    "AuthKeyUnregistered": "Сессия недействительна — нужна повторная авторизация",
    "FloodWaitError": "Telegram требует подождать (слишком частые запросы)",
    "ConnectionError": "Нет соединения — проверьте интернет и прокси",
    "TimeoutError": "Превышено время ожидания — прокси медленный или недоступен",
    "UNIQUE constraint failed": "Аккаунт уже существует в базе данных",
    "Session not authorized": "Сессия истекла — нужна повторная авторизация в Telethon",
    "Browser launch timeout": "Браузер не запустился — попробуйте снова",
    "Proxy relay not responding": "Прокси-релей не отвечает — проверьте прокси",
}


def humanize_error(error: Optional[str]) -> str:
    """Convert technical error to human-readable Russian message."""
    if not error:
        return "Неизвестная ошибка"
    for key, message in _ERROR_MAP.items():
        if key in error:
            return message
    return error


@dataclass
class AccountResult:
    """Result of a single account migration attempt."""
    account_id: int
    account_name: str
    success: bool
    error: Optional[str] = None
    retries_used: int = 0


@dataclass
class PoolResult:
    """Aggregate result of a worker pool run."""
    total: int = 0
    success_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    results: list[AccountResult] = field(default_factory=list)


# Sentinel value to signal workers to stop
_STOP_SENTINEL = -1


class MigrationWorkerPool:
    """
    Asyncio Queue-based worker pool for parallel account migration.

    Usage::

        pool = MigrationWorkerPool(db=db, num_workers=3)
        result = await pool.run(account_ids)
        # result.success_count, result.error_count, etc.

        # To stop early:
        pool.request_shutdown()
    """

    def __init__(
        self,
        db: Database,
        num_workers: int = 3,
        cooldown_range: tuple[float, float] = (60.0, 120.0),
        batch_pause_every: int = 10,
        batch_pause_range: tuple[float, float] = (120.0, 180.0),
        max_retries: int = 2,
        task_timeout: float = 300.0,
        resource_monitor: Optional[ResourceMonitor] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        password_2fa: Optional[str] = None,
        headless: bool = True,
        on_progress: Optional[Callable[[int, int, Optional[AccountResult]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        mode: str = "web",
    ) -> None:
        """
        Args:
            db: Database instance for account/proxy lookups and status updates.
            num_workers: Number of parallel worker coroutines (1-8).
            cooldown_range: (min, max) seconds to sleep between migrations per worker.
            batch_pause_every: Pause all workers every N completed accounts.
            batch_pause_range: (min, max) seconds for batch pause.
            max_retries: Max retry attempts for transient errors per account.
            task_timeout: Timeout in seconds for a single migration.
            resource_monitor: Optional ResourceMonitor instance.
            circuit_breaker: Optional CircuitBreaker instance.
            password_2fa: 2FA password for accounts that need it.
            headless: Run browser in headless mode.
            on_progress: Callback(completed, total, latest_result).
            on_log: Callback(message) for log output.
            mode: "web" for QR login migration, "fragment" for fragment.com auth.
        """
        self._db = db
        self._num_workers = max(1, min(num_workers, 20))
        self._cooldown_range = cooldown_range
        self._batch_pause_every = batch_pause_every
        self._batch_pause_range = batch_pause_range
        self._max_retries = max_retries
        self._task_timeout = task_timeout
        self._resource_monitor = resource_monitor or ResourceMonitor()
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=5, reset_timeout=60.0
        )
        self._password_2fa = password_2fa
        self._headless = headless
        self._on_progress = on_progress
        self._on_log = on_log
        self._mode = mode
        self._browser_manager = BrowserManager()

        self._shutdown_event = asyncio.Event()
        # Unlimited queue: retries go back into the queue, bounded buffer
        # would drop retries after 30s timeout when full. Memory is trivial
        # for 1000 ints. Backpressure is naturally regulated by worker count.
        self._queue: asyncio.Queue[int] = asyncio.Queue()

        # Shared counter for completed items
        self._completed_count = 0

        # Retry tracking: account_id -> attempts_used
        self._retry_counts: dict[int, int] = {}

        # FIX #5: Shared event for batch pause — all workers wait on this.
        # Set = running, cleared = paused.
        self._batch_pause_event = asyncio.Event()
        self._batch_pause_event.set()  # Start in running state

    def request_shutdown(self) -> None:
        """Request graceful shutdown. Workers finish current task and exit."""
        self._shutdown_event.set()
        self._log("[Pool] Shutdown requested - finishing active migrations...")

    async def run(self, account_ids: list[int]) -> PoolResult:
        """
        Run the migration pool on the given account IDs.

        Args:
            account_ids: List of account database IDs to migrate.

        Returns:
            PoolResult with aggregate statistics.
        """
        if not account_ids:
            return PoolResult()

        # FIX #6: Deduplicate to prevent two workers opening same .session
        # (AUTH_KEY_DUPLICATED = session death). Preserves order.
        account_ids = list(dict.fromkeys(account_ids))

        self._shutdown_event.clear()
        self._completed_count = 0
        self._retry_counts.clear()
        # Recreate queue to ensure clean state (unlimited — see __init__ comment)
        self._queue = asyncio.Queue()
        # FIX #5: Reset batch pause event to running state
        self._batch_pause_event = asyncio.Event()
        self._batch_pause_event.set()

        total = len(account_ids)
        result = PoolResult(total=total)

        self._log(
            f"[Pool] Starting migration: {total} accounts, "
            f"{self._num_workers} workers"
        )

        # FIX-C: Wrap worker execution in try/finally to ensure BrowserManager
        # cleanup on any exception (prevents zombie browsers on pool crash/cancel).
        try:
            # Create producer and worker tasks
            producer = asyncio.create_task(self._producer(account_ids))
            workers = [
                asyncio.create_task(self._worker(i, total, result))
                for i in range(self._num_workers)
            ]

            # Wait for producer to finish feeding the queue
            await producer

            # FIX #12: queue.join() with timeout to prevent hanging if a worker crashes.
            # Timeout = (task_timeout * num_workers) + 60s buffer.
            join_timeout = self._task_timeout * self._num_workers + 60
            try:
                await asyncio.wait_for(self._queue.join(), timeout=join_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Queue join timed out after %.0fs — sending stop sentinels anyway",
                    join_timeout,
                )

            # Ensure workers aren't stuck on batch pause before sending sentinels.
            # Without this, workers blocked on batch_pause_event.wait() would never
            # consume the sentinels, causing asyncio.gather to hang.
            self._batch_pause_event.set()

            # Send stop sentinels for each worker
            for _ in range(self._num_workers):
                await self._queue.put(_STOP_SENTINEL)

            # Wait for all workers to finish
            await asyncio.gather(*workers)

            self._log(
                f"[Pool] Complete: {result.success_count} OK, "
                f"{result.error_count} errors, "
                f"{result.skipped_count} skipped, "
                f"{result.total} total"
            )

            return result
        finally:
            try:
                await self._browser_manager.close_all()
            except Exception as e:
                logger.warning("BrowserManager cleanup error: %s", e)

    async def _producer(self, account_ids: list[int]) -> None:
        """Feed account IDs into the queue, respecting shutdown."""
        for account_id in account_ids:
            if self._shutdown_event.is_set():
                self._log("[Pool] Producer stopping (shutdown requested)")
                break
            await self._queue.put(account_id)

    async def _worker(
        self, worker_id: int, total: int, result: PoolResult
    ) -> None:
        """
        Worker coroutine: consume account IDs from queue and migrate.

        Args:
            worker_id: Worker identifier for logging.
            total: Total number of accounts for progress display.
            result: Shared PoolResult to record outcomes.
        """
        while True:
            # FIX #5: Wait for batch pause to clear before taking next item.
            # All workers block here when batch pause is active.
            await self._batch_pause_event.wait()

            account_id = await self._queue.get()

            if account_id == _STOP_SENTINEL:
                self._queue.task_done()
                break

            if self._shutdown_event.is_set():
                self._queue.task_done()
                continue  # Drain queue without processing

            # FIX-D: task_done() in finally block — prevents queue.join() deadlock
            # if any exception occurs in result recording, progress callback, or cooldown.
            try:
                try:
                    account_result = await self._process_account(
                        worker_id, total, account_id
                    )
                except Exception as exc:
                    logger.error(
                        "[W%d] Unhandled error processing account %d: %s",
                        worker_id, account_id, exc,
                    )
                    # FIX: Mark account as error in DB so it doesn't stay as "migrating"
                    try:
                        await self._db.update_account(
                            account_id, status="error",
                            error_message=f"Internal error: {exc}"
                        )
                    except Exception:
                        pass  # DB update is best-effort
                    account_result = AccountResult(
                        account_id=account_id,
                        account_name=f"id={account_id}",
                        success=False,
                        error=f"Internal error: {exc}",
                    )

                # Record result (only count non-retry results)
                is_retry = (
                    account_result.error
                    and account_result.error.startswith("RETRY:")
                )
                if not is_retry:
                    result.results.append(account_result)
                    if account_result.success:
                        result.success_count += 1
                    elif account_result.error and account_result.error.startswith("SKIP"):
                        result.skipped_count += 1
                    else:
                        result.error_count += 1

                    # Update completed count only for final results (not retries).
                    # Counting retries inflates progress and triggers batch pause
                    # too frequently (every 3-4 accounts instead of every 10).
                    self._completed_count += 1

                completed = self._completed_count

                if self._on_progress and not is_retry:
                    try:
                        self._on_progress(completed, total, account_result)
                    except Exception as exc:
                        logger.warning("Progress callback error: %s", exc)

                # Cooldown (skip on shutdown and retries)
                if not self._shutdown_event.is_set() and not is_retry:
                    await self._cooldown(
                        completed,
                        is_flood_wait="flood" in (account_result.error or "").lower(),
                    )
            finally:
                self._queue.task_done()

    async def _process_account(
        self, worker_id: int, total: int, account_id: int
    ) -> AccountResult:
        """
        Process a single account: fetch from DB, migrate, update status.

        Handles circuit breaker, resource checks, retries, and timeouts.

        Args:
            worker_id: Worker identifier for logging.
            total: Total number of accounts for progress display.
            account_id: Database ID of the account to migrate.

        Returns:
            AccountResult indicating success, failure, skip, or retry.
        """
        # Fetch account from DB
        account = await self._db.get_account(account_id)
        if not account:
            return AccountResult(
                account_id=account_id,
                account_name=f"id={account_id}",
                success=False,
                error="SKIP: Account not found in DB",
            )

        name = account.name

        # Wait for circuit breaker if open
        if not self._circuit_breaker.can_proceed():
            wait_time = self._circuit_breaker.time_until_reset()
            self._log(
                f"[W{worker_id}] Circuit breaker open, "
                f"waiting {wait_time:.0f}s..."
            )
            await self._interruptible_sleep(wait_time)
            if self._shutdown_event.is_set():
                return AccountResult(
                    account_id=account_id,
                    account_name=name,
                    success=False,
                    error="SKIP: Shutdown during circuit breaker wait",
                )

        # FIX #4: In half-open state, only one worker probes.
        # acquire_half_open_probe() returns False if another worker is already probing.
        if self._circuit_breaker.is_open:
            if not await self._circuit_breaker.acquire_half_open_probe():
                # Another worker is probing — wait for probe result
                wait_time = self._circuit_breaker.time_until_reset() or 5.0
                self._log(
                    f"[W{worker_id}] Circuit breaker half-open, "
                    f"another worker is probing — waiting {wait_time:.0f}s..."
                )
                await self._interruptible_sleep(wait_time)
                # Re-check after wait — the probe worker may have closed the circuit
                if not self._circuit_breaker.can_proceed():
                    return AccountResult(
                        account_id=account_id,
                        account_name=name,
                        success=False,
                        error="RETRY: Circuit breaker still open after probe",
                    )

        # Check resource availability
        if not self._resource_monitor.can_launch_more():
            self._log(
                f"[W{worker_id}] Resources exhausted, "
                f"waiting 30s for {name}..."
            )
            await self._interruptible_sleep(30.0)
            if not self._resource_monitor.can_launch_more():
                return AccountResult(
                    account_id=account_id,
                    account_name=name,
                    success=False,
                    error="SKIP: Resources exhausted after wait",
                )

        # Build proxy string + validate proxy availability
        proxy_str = await self._build_proxy_string(account)
        if account.proxy_id and not proxy_str:
            # Proxy was assigned but disappeared/dead — fail fast instead
            # of launching browser without proxy (which triggers circuit breaker
            # cascade when all proxies die mid-batch).
            error_msg = f"Proxy unavailable for {name}. Run: python -m src.cli proxy-refresh -f proxies.txt"
            self._log(f"[W{worker_id}] {name} - {error_msg}")
            try:
                await self._db.update_account(
                    account_id, status="error",
                    error_message="Proxy unavailable — run proxy-refresh"
                )
            except Exception as exc:
                logger.warning("DB update failed for %s: %s", name, exc)
            return AccountResult(
                account_id=account_id,
                account_name=name,
                success=False,
                error=error_msg,
            )

        # Validate session dir exists
        session_path = Path(account.session_path)
        session_dir = session_path.parent
        if not session_dir.exists():
            self._log(f"[W{worker_id}] {name} - SKIP (session dir not found)")
            try:
                await self._db.update_account(
                    account_id, status="error",
                    error_message="Session dir not found"
                )
            except Exception as exc:
                logger.warning("DB update failed for %s: %s", name, exc)
            return AccountResult(
                account_id=account_id,
                account_name=name,
                success=False,
                error="SKIP: Session dir not found",
            )

        retries = self._retry_counts.get(account_id, 0)

        # Start migration tracking in DB (web mode only — fragment mode
        # must NOT overwrite account status or pollute migrations table)
        migration_id: Optional[int] = None
        if self._mode != "fragment":
            try:
                migration_id = await self._db.start_migration(account_id)
            except Exception as exc:
                logger.warning("DB start_migration failed for %s: %s", name, exc)
                return AccountResult(
                    account_id=account_id,
                    account_name=name,
                    success=False,
                    error=f"DB error: {exc}",
                    retries_used=retries,
                )

        self._log(
            f"[W{worker_id}] [{self._completed_count + 1}/{total}] "
            f"{name}{'  (retry #' + str(retries) + ')' if retries else ''}..."
        )

        # Run migration/fragment with timeout
        migrate_fn = fragment_account if self._mode == "fragment" else migrate_account
        try:
            auth_result: AuthResult = await asyncio.wait_for(
                migrate_fn(
                    account_dir=session_dir,
                    password_2fa=self._password_2fa,
                    headless=self._headless,
                    proxy_override=proxy_str,
                    browser_manager=self._browser_manager,
                ),
                timeout=self._task_timeout,
            )
        except asyncio.TimeoutError:
            error_msg = f"Timeout after {self._task_timeout:.0f}s"
            self._log(f"[W{worker_id}] {name} - TIMEOUT")
            if migration_id is not None:
                await self._complete_migration_safe(
                    migration_id, name, success=False, error_message=error_msg
                )
            elif self._mode == "fragment":
                await self._update_fragment_status_safe(
                    account_id, name, "error", error_msg
                )
            self._circuit_breaker.record_failure()
            self._circuit_breaker.release_half_open_probe()  # FIX #4
            return await self._maybe_retry(
                account_id, name, error_msg, retries
            )
        except Exception as exc:
            error_msg = str(exc)
            self._log(f"[W{worker_id}] {name} - ERROR: {humanize_error(error_msg)}")
            if migration_id is not None:
                await self._complete_migration_safe(
                    migration_id, name, success=False, error_message=error_msg
                )
            elif self._mode == "fragment":
                await self._update_fragment_status_safe(
                    account_id, name, "error", error_msg
                )
            self._circuit_breaker.record_failure()
            self._circuit_breaker.release_half_open_probe()  # FIX #4
            return await self._maybe_retry(
                account_id, name, error_msg, retries
            )

        # Process result
        if auth_result.success:
            if self._mode == "fragment":
                await self._update_fragment_status_safe(
                    account_id, name, "authorized"
                )
            else:
                username = (
                    auth_result.user_info.get("username")
                    if auth_result.user_info else None
                )
                profile_path = (
                    str(PROFILES_DIR / auth_result.profile_name)
                    if auth_result.profile_name else None
                )
                await self._complete_migration_safe(
                    migration_id, name, success=True, profile_path=profile_path
                )
                if username:
                    try:
                        await self._db.update_account(
                            account_id, username=username
                        )
                    except Exception as exc:
                        logger.warning("DB update username for %s: %s", name, exc)
            self._circuit_breaker.record_success()
            self._circuit_breaker.release_half_open_probe()  # FIX #4
            self._log(f"[W{worker_id}] {name} - OK")
            return AccountResult(
                account_id=account_id,
                account_name=name,
                success=True,
                retries_used=retries,
            )
        else:
            error_msg = auth_result.error or "Unknown error"
            if migration_id is not None:
                await self._complete_migration_safe(
                    migration_id, name, success=False, error_message=error_msg
                )
            elif self._mode == "fragment":
                await self._update_fragment_status_safe(
                    account_id, name, "error", error_msg
                )
            self._circuit_breaker.record_failure()
            self._circuit_breaker.release_half_open_probe()  # FIX #4
            self._log(f"[W{worker_id}] {name} - FAILED: {humanize_error(error_msg)}")
            return await self._maybe_retry(
                account_id, name, error_msg, retries
            )

    async def _complete_migration_safe(
        self,
        migration_id: int,
        name: str,
        success: bool,
        error_message: Optional[str] = None,
        profile_path: Optional[str] = None,
    ) -> None:
        """Wrapper around db.complete_migration that won't crash the worker."""
        try:
            await self._db.complete_migration(
                migration_id,
                success=success,
                error_message=error_message,
                profile_path=profile_path,
            )
        except Exception as exc:
            logger.error(
                "DB complete_migration failed for %s (migration_id=%d): %s",
                name, migration_id, exc,
            )

    async def _update_fragment_status_safe(
        self,
        account_id: int,
        name: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Update fragment_status without touching account status. Won't crash the worker."""
        try:
            kwargs: dict = {"fragment_status": status}
            if error_message:
                kwargs["error_message"] = error_message
            await self._db.update_account(account_id, **kwargs)
        except Exception as exc:
            logger.error(
                "DB update fragment_status failed for %s: %s", name, exc,
            )

    # Terminal errors that should NOT be retried — retrying wastes time and
    # pollutes the circuit breaker with expected failures.
    NON_RETRYABLE_PATTERNS = (
        "phonenumberbanned", "userdeactivated", "authkeyunregistered",
        "session is not authorized", "not authorized", "dead session",
        "sessionpasswordneeded", "2fa required", "2fa password",
        "unique constraint", "auth_key_duplicated",
    )

    def _is_retryable(self, error: str) -> bool:
        """Check if an error is transient and worth retrying."""
        error_lower = error.lower()
        for pattern in self.NON_RETRYABLE_PATTERNS:
            if pattern in error_lower:
                return False
        return True

    async def _maybe_retry(
        self,
        account_id: int,
        name: str,
        error: str,
        retries_used: int,
    ) -> AccountResult:
        """Re-enqueue account for retry if under max_retries, else return failure.

        On successful re-enqueue, returns a RETRY:-prefixed result so the worker
        knows not to count it as final. The re-enqueued item gets its own
        queue.task_done() call when processed.
        """
        # FIX: Skip retry for terminal errors (dead sessions, banned, 2FA, etc.)
        if not self._is_retryable(error):
            self._log(f"[Pool] {name} - non-retryable error, no retry: {error[:80]}")
            return AccountResult(
                account_id=account_id,
                account_name=name,
                success=False,
                error=error,
                retries_used=retries_used,
            )

        if retries_used < self._max_retries and not self._shutdown_event.is_set():
            self._retry_counts[account_id] = retries_used + 1
            self._log(
                f"[Pool] {name} - scheduling retry "
                f"#{retries_used + 1}/{self._max_retries}"
            )
            try:
                await asyncio.wait_for(
                    self._queue.put(account_id), timeout=30.0
                )
            except asyncio.TimeoutError:
                self._log(f"[Pool] {name} - queue full after 30s, retry dropped")
                return AccountResult(
                    account_id=account_id,
                    account_name=name,
                    success=False,
                    error=error,
                    retries_used=retries_used,
                )
            return AccountResult(
                account_id=account_id,
                account_name=name,
                success=False,
                error=f"RETRY: {error}",
                retries_used=retries_used,
            )

        return AccountResult(
            account_id=account_id,
            account_name=name,
            success=False,
            error=error,
            retries_used=retries_used,
        )

    async def _build_proxy_string(
        self, account: AccountRecord
    ) -> Optional[str]:
        """Build proxy connection string from DB proxy record."""
        if not account.proxy_id:
            return None
        proxy = await self._db.get_proxy(account.proxy_id)
        if not proxy:
            return None
        if proxy.username and proxy.password:
            return (
                f"{proxy.protocol}:{proxy.host}:{proxy.port}"
                f":{proxy.username}:{proxy.password}"
            )
        return f"{proxy.protocol}:{proxy.host}:{proxy.port}"

    async def _cooldown(
        self, completed_total: int, is_flood_wait: bool = False
    ) -> None:
        """
        Apply per-worker cooldown and global batch pause.

        FIX #5: Batch pause uses shared asyncio.Event to pause ALL workers,
        not just the one whose modulo count happens to hit the threshold.

        Args:
            completed_total: Total completed across all workers.
            is_flood_wait: If True, triple the cooldown.
        """
        # FIX #5: Batch pause check — CLEAR event to block ALL workers,
        # then SET after pause to release them.
        if (
            self._batch_pause_every > 0
            and completed_total > 0
            and completed_total % self._batch_pause_every == 0
        ):
            pause = random.uniform(*self._batch_pause_range)
            self._log(
                f"[Pool] Batch pause {pause / 60:.1f} min "
                f"(every {self._batch_pause_every} accounts)..."
            )
            # Clear event — all workers will block at top of their loop
            self._batch_pause_event.clear()
            await self._interruptible_sleep(pause)
            # Set event — release all workers
            self._batch_pause_event.set()
            return  # Batch pause replaces regular cooldown

        # Regular per-worker cooldown
        base = random.uniform(*self._cooldown_range)
        cooldown = base * 3 if is_flood_wait else base
        if is_flood_wait:
            self._log(
                f"[Pool] FLOOD_WAIT — увеличенная пауза "
                f"{cooldown:.0f}с (антибан)"
            )
        else:
            self._log(
                f"[Pool] Пауза {cooldown:.0f}с между аккаунтами (антибан)..."
            )
        await self._interruptible_sleep(cooldown)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that can be interrupted by shutdown event."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(), timeout=seconds
            )
        except asyncio.TimeoutError:
            pass  # Normal: timeout means sleep completed without shutdown

    def _log(self, message: str) -> None:
        """Send message to log callback and logger."""
        logger.info(message)
        if self._on_log:
            try:
                self._on_log(message)
            except Exception as exc:
                logger.warning("Log callback error: %s", exc)
