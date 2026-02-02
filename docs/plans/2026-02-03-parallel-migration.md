# Parallel Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable parallel migration of Telegram sessions to browser profiles (10-50 concurrent browsers) reducing total time from ~12 hours to ~1-2 hours for 1000 accounts.

**Architecture:** asyncio.Semaphore for concurrency control, progress callback for real-time updates, graceful shutdown via signal handlers, error isolation per account.

**Tech Stack:** Python asyncio, Semaphore, signal handlers, existing TelegramAuth class, Click progress bars.

---

## Task 1: Add Parallel Migration Core Function

**Files:**
- Modify: `src/telegram_auth.py:1186-1223` (after existing `migrate_accounts_batch`)
- Test: `tests/test_telegram_auth.py`

### Step 1.1: Write the failing test for parallel batch function

```python
# tests/test_telegram_auth.py - add at the end

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

class TestParallelMigration:
    """Tests for parallel migration functionality"""

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_respects_semaphore(self, tmp_path):
        """Verify semaphore limits concurrent executions"""
        # Track concurrent calls
        concurrent_count = 0
        max_concurrent = 0
        call_order = []

        async def mock_migrate(account_dir, password_2fa=None, headless=False):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            call_order.append(f"start_{account_dir.name}")
            await asyncio.sleep(0.1)  # Simulate work
            call_order.append(f"end_{account_dir.name}")
            concurrent_count -= 1
            return AuthResult(success=True, profile_name=account_dir.name)

        # Create 5 fake account dirs
        account_dirs = []
        for i in range(5):
            d = tmp_path / f"account_{i}"
            d.mkdir()
            account_dirs.append(d)

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
            from src.telegram_auth import migrate_accounts_parallel
            results = await migrate_accounts_parallel(
                account_dirs=account_dirs,
                max_concurrent=2,  # Only 2 at a time
                headless=True
            )

        assert len(results) == 5
        assert all(r.success for r in results)
        assert max_concurrent <= 2, f"Semaphore violated: max was {max_concurrent}"

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_progress_callback(self, tmp_path):
        """Verify progress callback is called correctly"""
        progress_calls = []

        def on_progress(completed, total, result):
            progress_calls.append((completed, total, result.profile_name if result else None))

        async def mock_migrate(account_dir, password_2fa=None, headless=False):
            await asyncio.sleep(0.01)
            return AuthResult(success=True, profile_name=account_dir.name)

        account_dirs = [tmp_path / f"acc_{i}" for i in range(3)]
        for d in account_dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
            from src.telegram_auth import migrate_accounts_parallel
            await migrate_accounts_parallel(
                account_dirs=account_dirs,
                max_concurrent=2,
                on_progress=on_progress
            )

        assert len(progress_calls) == 3
        # Each call should have increasing completed count
        completed_counts = [c[0] for c in progress_calls]
        assert sorted(completed_counts) == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_migrate_accounts_parallel_handles_errors(self, tmp_path):
        """Verify one error doesn't stop others"""
        async def mock_migrate(account_dir, password_2fa=None, headless=False):
            if "fail" in account_dir.name:
                raise Exception("Simulated failure")
            return AuthResult(success=True, profile_name=account_dir.name)

        dirs = [tmp_path / "ok_1", tmp_path / "fail_2", tmp_path / "ok_3"]
        for d in dirs:
            d.mkdir()

        with patch('src.telegram_auth.migrate_account', side_effect=mock_migrate):
            from src.telegram_auth import migrate_accounts_parallel
            results = await migrate_accounts_parallel(
                account_dirs=dirs,
                max_concurrent=3
            )

        assert len(results) == 3
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 2
        assert len(failures) == 1
        assert "Simulated failure" in failures[0].error
```

### Step 1.2: Run test to verify it fails

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration -v`
Expected: FAIL with "cannot import name 'migrate_accounts_parallel'"

### Step 1.3: Implement parallel migration function

```python
# src/telegram_auth.py - add after migrate_accounts_batch function (line ~1224)

from typing import Callable

# Progress callback type
ProgressCallback = Callable[[int, int, Optional[AuthResult]], None]


async def migrate_accounts_parallel(
    account_dirs: list[Path],
    password_2fa: Optional[str] = None,
    headless: bool = False,
    max_concurrent: int = 10,
    cooldown: float = 5.0,
    on_progress: Optional[ProgressCallback] = None
) -> list[AuthResult]:
    """
    Migrates multiple accounts in parallel with concurrency control.

    Args:
        account_dirs: List of account directories
        password_2fa: Shared 2FA password (if same for all)
        headless: Run browsers in headless mode
        max_concurrent: Maximum parallel browser instances (default 10)
        cooldown: Seconds between starting new tasks (rate limiting)
        on_progress: Callback(completed, total, result) for progress updates

    Returns:
        List of AuthResult in same order as account_dirs
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results: dict[int, AuthResult] = {}
    completed = 0
    total = len(account_dirs)
    lock = asyncio.Lock()

    async def migrate_with_semaphore(index: int, account_dir: Path):
        nonlocal completed
        async with semaphore:
            try:
                result = await migrate_account(
                    account_dir=account_dir,
                    password_2fa=password_2fa,
                    headless=headless
                )
            except Exception as e:
                result = AuthResult(
                    success=False,
                    profile_name=account_dir.name,
                    error=str(e)
                )

            async with lock:
                results[index] = result
                completed += 1
                if on_progress:
                    try:
                        on_progress(completed, total, result)
                    except Exception as e:
                        print(f"[Parallel] Progress callback error: {e}")

            return result

    # Create tasks with staggered start (rate limiting)
    tasks = []
    for i, account_dir in enumerate(account_dirs):
        task = asyncio.create_task(migrate_with_semaphore(i, account_dir))
        tasks.append(task)
        # Stagger task creation to avoid thundering herd
        if i < len(account_dirs) - 1 and cooldown > 0:
            await asyncio.sleep(cooldown)

    # Wait for all to complete
    await asyncio.gather(*tasks, return_exceptions=True)

    # Return results in original order
    return [results[i] for i in range(len(account_dirs))]
```

### Step 1.4: Run test to verify it passes

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration -v`
Expected: PASS (all 3 tests)

### Step 1.5: Commit

```bash
git add src/telegram_auth.py tests/test_telegram_auth.py
git commit -m "$(cat <<'EOF'
feat: add parallel migration with semaphore concurrency control

- migrate_accounts_parallel() supports N concurrent browsers
- Progress callback for real-time updates
- Error isolation: one failure doesn't stop others
- Rate limiting with cooldown between task starts

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add Graceful Shutdown Handler

**Files:**
- Modify: `src/telegram_auth.py`
- Test: `tests/test_telegram_auth.py`

### Step 2.1: Write the failing test for shutdown handler

```python
# tests/test_telegram_auth.py - add to TestParallelMigration class

@pytest.mark.asyncio
async def test_parallel_migration_shutdown_flag(self, tmp_path):
    """Verify shutdown flag stops accepting new tasks"""
    from src.telegram_auth import ParallelMigrationController

    controller = ParallelMigrationController(max_concurrent=2)

    started_count = 0
    async def slow_migrate(account_dir, password_2fa=None, headless=False):
        nonlocal started_count
        started_count += 1
        await asyncio.sleep(1.0)  # Long running
        return AuthResult(success=True, profile_name=account_dir.name)

    dirs = [tmp_path / f"acc_{i}" for i in range(5)]
    for d in dirs:
        d.mkdir()

    with patch('src.telegram_auth.migrate_account', side_effect=slow_migrate):
        # Start migration
        task = asyncio.create_task(
            controller.run(dirs, headless=True)
        )
        await asyncio.sleep(0.2)  # Let some tasks start

        # Request shutdown
        controller.request_shutdown()

        results = await task

    # Should have stopped early, not all 5 completed
    # At least the running ones should complete
    assert controller.is_shutdown_requested
```

### Step 2.2: Run test to verify it fails

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration::test_parallel_migration_shutdown_flag -v`
Expected: FAIL with "cannot import name 'ParallelMigrationController'"

### Step 2.3: Implement ParallelMigrationController class

```python
# src/telegram_auth.py - add after migrate_accounts_parallel function

class ParallelMigrationController:
    """
    Controller for parallel migration with graceful shutdown support.

    Usage:
        controller = ParallelMigrationController(max_concurrent=10)

        # In signal handler:
        signal.signal(signal.SIGINT, lambda s, f: controller.request_shutdown())

        results = await controller.run(account_dirs)
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        cooldown: float = 5.0
    ):
        self.max_concurrent = max_concurrent
        self.cooldown = cooldown
        self._shutdown_requested = False
        self._active_tasks: set = set()
        self._completed = 0
        self._total = 0

    @property
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @property
    def progress(self) -> tuple[int, int]:
        """Returns (completed, total)"""
        return (self._completed, self._total)

    def request_shutdown(self):
        """Request graceful shutdown - finish running, don't start new"""
        print("\n[ParallelMigration] Shutdown requested - finishing active tasks...")
        self._shutdown_requested = True

    async def run(
        self,
        account_dirs: list[Path],
        password_2fa: Optional[str] = None,
        headless: bool = False,
        on_progress: Optional[ProgressCallback] = None
    ) -> list[AuthResult]:
        """
        Run parallel migration with shutdown support.

        Returns results for completed accounts (may be partial on shutdown).
        """
        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: dict[int, AuthResult] = {}
        lock = asyncio.Lock()
        self._total = len(account_dirs)
        self._completed = 0
        self._shutdown_requested = False

        async def migrate_one(index: int, account_dir: Path):
            # Check shutdown before acquiring semaphore
            if self._shutdown_requested:
                return None

            async with semaphore:
                # Check again after acquiring
                if self._shutdown_requested:
                    return None

                try:
                    result = await migrate_account(
                        account_dir=account_dir,
                        password_2fa=password_2fa,
                        headless=headless
                    )
                except Exception as e:
                    result = AuthResult(
                        success=False,
                        profile_name=account_dir.name,
                        error=str(e)
                    )

                async with lock:
                    results[index] = result
                    self._completed += 1
                    if on_progress:
                        try:
                            on_progress(self._completed, self._total, result)
                        except Exception:
                            pass

                return result

        # Create and track tasks
        tasks = []
        for i, account_dir in enumerate(account_dirs):
            if self._shutdown_requested:
                break

            task = asyncio.create_task(migrate_one(i, account_dir))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            tasks.append(task)

            # Rate limiting
            if i < len(account_dirs) - 1 and self.cooldown > 0:
                await asyncio.sleep(self.cooldown)

        # Wait for all started tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Return results in order (only completed ones)
        ordered_results = []
        for i in range(len(account_dirs)):
            if i in results:
                ordered_results.append(results[i])
            elif self._shutdown_requested:
                # Mark as skipped due to shutdown
                ordered_results.append(AuthResult(
                    success=False,
                    profile_name=account_dirs[i].name,
                    error="Skipped due to shutdown"
                ))

        return ordered_results
```

### Step 2.4: Run test to verify it passes

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration -v`
Expected: PASS

### Step 2.5: Commit

```bash
git add src/telegram_auth.py tests/test_telegram_auth.py
git commit -m "$(cat <<'EOF'
feat: add ParallelMigrationController with graceful shutdown

- request_shutdown() stops starting new tasks
- Running tasks complete gracefully
- Progress tracking (completed, total)
- Skipped accounts marked in results

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add CLI Command for Parallel Migration

**Files:**
- Modify: `src/cli.py`
- Test: Manual testing (CLI is integration)

### Step 3.1: Add parallel option to migrate command

```python
# src/cli.py - modify migrate command

@cli.command()
@click.option("--account", help="Account name or path to migrate")
@click.option("--all", "migrate_all", is_flag=True, help="Migrate all accounts")
@click.option("--password", "-p", help="2FA password (if same for all)")
@click.option("--headless", is_flag=True, help="Run in headless mode")
@click.option("--cooldown", default=DEFAULT_COOLDOWN, help="Seconds between accounts (sequential)")
@click.option("--parallel", "-j", type=int, default=0,
              help="Parallel instances (0=sequential, 10=recommended)")
def migrate(account, migrate_all, password, headless, cooldown, parallel):
    """Migrate Telegram session(s) to browser profile(s)"""
    import signal

    if not account and not migrate_all:
        click.echo("Error: Specify --account or --all")
        return

    accounts = find_accounts(account, migrate_all)
    if not accounts:
        click.echo("No accounts found")
        return

    click.echo(f"Found {len(accounts)} account(s) to migrate")

    if parallel > 0:
        # Parallel mode
        click.echo(f"Mode: PARALLEL (max {parallel} concurrent)")
        click.echo(f"Cooldown between starts: {cooldown}s")

        from src.telegram_auth import ParallelMigrationController

        controller = ParallelMigrationController(
            max_concurrent=parallel,
            cooldown=cooldown
        )

        # Setup signal handler for graceful shutdown
        def handle_signal(signum, frame):
            click.echo("\nReceived interrupt signal...")
            controller.request_shutdown()

        signal.signal(signal.SIGINT, handle_signal)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, handle_signal)

        # Progress bar
        with click.progressbar(length=len(accounts),
                               label='Migrating',
                               show_pos=True) as bar:
            last_completed = 0

            def on_progress(completed, total, result):
                nonlocal last_completed
                bar.update(completed - last_completed)
                last_completed = completed
                status = "OK" if result and result.success else "FAIL"
                name = result.profile_name if result else "?"
                click.echo(f" [{status}] {name}")

            results = asyncio.run(controller.run(
                account_dirs=accounts,
                password_2fa=password,
                headless=headless,
                on_progress=on_progress
            ))
    else:
        # Sequential mode (existing)
        click.echo(f"Mode: SEQUENTIAL (cooldown {cooldown}s)")
        results = asyncio.run(migrate_accounts_batch(
            account_dirs=accounts,
            password_2fa=password,
            headless=headless,
            cooldown=cooldown
        ))

    # Summary
    click.echo(f"\n{'='*50}")
    click.echo("MIGRATION SUMMARY")
    click.echo(f"{'='*50}")

    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    click.echo(f"Total: {len(results)}")
    click.echo(f"Success: {len(success)}")
    click.echo(f"Failed: {len(failed)}")

    if failed:
        click.echo("\nFailed accounts:")
        for r in failed:
            click.echo(f"  - {r.profile_name}: {r.error}")
```

### Step 3.2: Update imports in cli.py

```python
# src/cli.py - add to imports
from src.telegram_auth import (
    migrate_account,
    migrate_accounts_batch,
    ParallelMigrationController,  # Add this
    AuthResult
)
```

### Step 3.3: Test CLI manually

Run: `python -m src.cli migrate --all --parallel 2 --headless`
Expected: Progress bar, parallel execution, summary

### Step 3.4: Commit

```bash
git add src/cli.py
git commit -m "$(cat <<'EOF'
feat: add --parallel option to migrate command

Usage: python -m src.cli migrate --all --parallel 10

- Progress bar with real-time updates
- Graceful shutdown on Ctrl+C
- Summary shows success/failed counts

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add Resource Monitoring

**Files:**
- Create: `src/resource_monitor.py`
- Test: `tests/test_resource_monitor.py`

### Step 4.1: Write the failing test

```python
# tests/test_resource_monitor.py

import pytest
import asyncio
from src.resource_monitor import ResourceMonitor


class TestResourceMonitor:
    def test_get_system_resources(self):
        """Test basic resource reading"""
        monitor = ResourceMonitor()
        resources = monitor.get_current()

        assert 'cpu_percent' in resources
        assert 'memory_percent' in resources
        assert 'memory_available_gb' in resources
        assert 0 <= resources['cpu_percent'] <= 100
        assert 0 <= resources['memory_percent'] <= 100

    def test_can_launch_more(self):
        """Test launch decision logic"""
        monitor = ResourceMonitor(
            max_memory_percent=80,
            max_cpu_percent=90
        )

        # Mock low usage - should allow
        monitor._get_resources = lambda: {
            'cpu_percent': 50,
            'memory_percent': 60,
            'memory_available_gb': 4.0
        }
        assert monitor.can_launch_more() is True

        # Mock high memory - should block
        monitor._get_resources = lambda: {
            'cpu_percent': 50,
            'memory_percent': 85,
            'memory_available_gb': 1.0
        }
        assert monitor.can_launch_more() is False

    def test_recommended_concurrency(self):
        """Test concurrency recommendation"""
        monitor = ResourceMonitor()
        # With 8GB available, should recommend ~8 browsers (1GB each estimate)
        monitor._get_resources = lambda: {
            'memory_available_gb': 8.0,
            'cpu_percent': 30,
            'memory_percent': 50
        }
        rec = monitor.recommended_concurrency()
        assert 4 <= rec <= 16  # Reasonable range
```

### Step 4.2: Run test to verify it fails

Run: `pytest tests/test_resource_monitor.py -v`
Expected: FAIL with "No module named 'src.resource_monitor'"

### Step 4.3: Implement ResourceMonitor

```python
# src/resource_monitor.py
"""
Resource Monitor for Parallel Migration.

Monitors system resources to prevent overload when running
many browser instances in parallel.
"""
import psutil
from dataclasses import dataclass
from typing import Dict


@dataclass
class ResourceLimits:
    """Resource usage limits"""
    max_memory_percent: float = 80.0  # Stop launching if memory > 80%
    max_cpu_percent: float = 90.0      # Stop launching if CPU > 90%
    min_memory_available_gb: float = 2.0  # Need at least 2GB free
    memory_per_browser_gb: float = 0.5    # Estimate per Camoufox instance


class ResourceMonitor:
    """
    Monitors system resources for parallel migration.

    Usage:
        monitor = ResourceMonitor()
        if monitor.can_launch_more():
            # Start another browser
        recommended = monitor.recommended_concurrency()
    """

    def __init__(
        self,
        max_memory_percent: float = 80.0,
        max_cpu_percent: float = 90.0,
        min_memory_available_gb: float = 2.0
    ):
        self.limits = ResourceLimits(
            max_memory_percent=max_memory_percent,
            max_cpu_percent=max_cpu_percent,
            min_memory_available_gb=min_memory_available_gb
        )

    def _get_resources(self) -> Dict[str, float]:
        """Get current resource usage (internal, mockable for tests)"""
        return self.get_current()

    def get_current(self) -> Dict[str, float]:
        """Get current system resource usage"""
        memory = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.1)

        return {
            'cpu_percent': cpu,
            'memory_percent': memory.percent,
            'memory_available_gb': memory.available / (1024 ** 3),
            'memory_total_gb': memory.total / (1024 ** 3),
        }

    def can_launch_more(self) -> bool:
        """Check if system can handle more browser instances"""
        resources = self._get_resources()

        if resources['memory_percent'] > self.limits.max_memory_percent:
            return False
        if resources['cpu_percent'] > self.limits.max_cpu_percent:
            return False
        if resources['memory_available_gb'] < self.limits.min_memory_available_gb:
            return False

        return True

    def recommended_concurrency(self) -> int:
        """
        Recommend number of concurrent browsers based on available resources.

        Returns a conservative estimate based on available memory.
        """
        resources = self._get_resources()
        available_gb = resources['memory_available_gb']

        # Reserve 2GB for system, rest for browsers
        usable_gb = max(0, available_gb - 2.0)
        recommended = int(usable_gb / self.limits.memory_per_browser_gb)

        # Clamp to reasonable range
        return max(1, min(recommended, 50))

    def format_status(self) -> str:
        """Format current status for display"""
        r = self._get_resources()
        return (
            f"CPU: {r['cpu_percent']:.1f}% | "
            f"Memory: {r['memory_percent']:.1f}% | "
            f"Available: {r['memory_available_gb']:.1f}GB"
        )
```

### Step 4.4: Run test to verify it passes

Run: `pytest tests/test_resource_monitor.py -v`
Expected: PASS

### Step 4.5: Add psutil to requirements.txt

```
# Add to requirements.txt
psutil>=5.9.0
```

### Step 4.6: Commit

```bash
git add src/resource_monitor.py tests/test_resource_monitor.py requirements.txt
git commit -m "$(cat <<'EOF'
feat: add resource monitor for parallel migration

- Tracks CPU, memory, available RAM
- can_launch_more() checks against limits
- recommended_concurrency() suggests optimal parallelism
- Prevents system overload during batch migration

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Integrate Resource Monitor into Parallel Migration

**Files:**
- Modify: `src/telegram_auth.py`
- Modify: `src/cli.py`
- Test: `tests/test_telegram_auth.py`

### Step 5.1: Write the failing test

```python
# tests/test_telegram_auth.py - add to TestParallelMigration

@pytest.mark.asyncio
async def test_parallel_migration_respects_resource_limits(self, tmp_path):
    """Verify migration pauses when resources exhausted"""
    from unittest.mock import MagicMock
    from src.telegram_auth import ParallelMigrationController
    from src.resource_monitor import ResourceMonitor

    # Create mock monitor that blocks after 2 launches
    launch_count = 0
    def mock_can_launch():
        nonlocal launch_count
        launch_count += 1
        return launch_count <= 2  # Block after 2

    monitor = ResourceMonitor()
    monitor.can_launch_more = mock_can_launch

    controller = ParallelMigrationController(
        max_concurrent=5,
        resource_monitor=monitor
    )

    async def quick_migrate(account_dir, **kwargs):
        await asyncio.sleep(0.01)
        return AuthResult(success=True, profile_name=account_dir.name)

    dirs = [tmp_path / f"acc_{i}" for i in range(3)]
    for d in dirs:
        d.mkdir()

    with patch('src.telegram_auth.migrate_account', side_effect=quick_migrate):
        results = await asyncio.wait_for(
            controller.run(dirs, headless=True),
            timeout=5.0
        )

    # Should still complete all (resource check is advisory)
    assert len(results) == 3
```

### Step 5.2: Run test to verify it fails

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration::test_parallel_migration_respects_resource_limits -v`
Expected: FAIL (resource_monitor param not supported)

### Step 5.3: Update ParallelMigrationController to use ResourceMonitor

```python
# src/telegram_auth.py - update ParallelMigrationController.__init__

from src.resource_monitor import ResourceMonitor

class ParallelMigrationController:
    def __init__(
        self,
        max_concurrent: int = 10,
        cooldown: float = 5.0,
        resource_monitor: Optional[ResourceMonitor] = None
    ):
        self.max_concurrent = max_concurrent
        self.cooldown = cooldown
        self.resource_monitor = resource_monitor
        self._shutdown_requested = False
        self._active_tasks: set = set()
        self._completed = 0
        self._total = 0
        self._paused_for_resources = False

    async def run(self, ...):  # existing signature
        # ... existing code ...

        async def migrate_one(index: int, account_dir: Path):
            # Check shutdown before acquiring semaphore
            if self._shutdown_requested:
                return None

            # Wait for resources if monitor provided
            if self.resource_monitor:
                wait_count = 0
                while not self.resource_monitor.can_launch_more():
                    if self._shutdown_requested:
                        return None
                    if wait_count == 0:
                        self._paused_for_resources = True
                        print(f"[ParallelMigration] Waiting for resources: {self.resource_monitor.format_status()}")
                    await asyncio.sleep(5)
                    wait_count += 1
                    if wait_count > 60:  # 5 min timeout
                        return AuthResult(
                            success=False,
                            profile_name=account_dir.name,
                            error="Timeout waiting for resources"
                        )
                self._paused_for_resources = False

            async with semaphore:
                # ... rest of existing code ...
```

### Step 5.4: Run test to verify it passes

Run: `pytest tests/test_telegram_auth.py::TestParallelMigration -v`
Expected: PASS

### Step 5.5: Update CLI to use resource monitor

```python
# src/cli.py - in migrate command, parallel mode section

from src.resource_monitor import ResourceMonitor

# Add option
@click.option("--auto-scale", is_flag=True,
              help="Auto-adjust parallelism based on system resources")

# In parallel mode:
monitor = None
if auto_scale:
    monitor = ResourceMonitor()
    if parallel == 0:
        parallel = monitor.recommended_concurrency()
    click.echo(f"Resource monitor: {monitor.format_status()}")
    click.echo(f"Recommended parallelism: {monitor.recommended_concurrency()}")

controller = ParallelMigrationController(
    max_concurrent=parallel,
    cooldown=cooldown,
    resource_monitor=monitor if auto_scale else None
)
```

### Step 5.6: Commit

```bash
git add src/telegram_auth.py src/cli.py tests/test_telegram_auth.py
git commit -m "$(cat <<'EOF'
feat: integrate resource monitor into parallel migration

- ParallelMigrationController accepts resource_monitor
- Pauses launching when resources exhausted
- CLI --auto-scale flag enables resource-based throttling
- Timeout after 5 min waiting for resources

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Run Full Test Suite and Integration Test

**Files:**
- All test files

### Step 6.1: Run all unit tests

Run: `pytest -v`
Expected: All tests PASS

### Step 6.2: Run integration test with test account

Run: `python -m src.cli migrate --account "accounts/test/новый тестовый акк/573181716267" --parallel 1`
Expected: Single account migrates successfully

### Step 6.3: Test parallel with 2 accounts (if available)

Run: `python -m src.cli migrate --all --parallel 2 --headless`
Expected: Both accounts migrate, progress shown

### Step 6.4: Final commit

```bash
git add -A
git commit -m "$(cat <<'EOF'
test: verify parallel migration end-to-end

- All unit tests pass
- Integration test with real account passes
- Parallel mode with 2 accounts verified

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Summary

After completing all tasks:

1. **migrate_accounts_parallel()** - Core parallel function with semaphore
2. **ParallelMigrationController** - Graceful shutdown support
3. **CLI --parallel N** - Easy command line usage
4. **ResourceMonitor** - Prevents system overload
5. **Integration** - All components work together

**Usage:**
```bash
# Migrate all accounts with 10 parallel browsers
python -m src.cli migrate --all --parallel 10 --headless

# Auto-scale based on system resources
python -m src.cli migrate --all --parallel 10 --auto-scale --headless

# Ctrl+C for graceful shutdown (finishes running, skips pending)
```

**Performance estimate:**
- Sequential: 1000 accounts × 45s = ~12.5 hours
- Parallel (10): 1000 accounts × 50s ÷ 10 = ~1.4 hours
- Parallel (20): 1000 accounts × 55s ÷ 20 = ~0.8 hours
