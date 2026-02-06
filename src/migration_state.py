"""
Migration State Persistence Module

Provides JSON-based persistence for batch migration state.
Enables resume after crash and retry of failed accounts.

FIX-004: Atomic writes with file locking to prevent race conditions.
"""
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

# FIX-004: Platform-specific file locking
if sys.platform == 'win32':
    import msvcrt

    def _lock_file(f):
        """Acquire exclusive lock on file (Windows)"""
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(f):
        """Release file lock (Windows)"""
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
else:
    import fcntl

    def _lock_file(f):
        """Acquire exclusive lock on file (Unix)"""
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(f):
        """Release file lock (Unix)"""
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass


@dataclass
class FailedAccount:
    """Record of a failed migration attempt"""
    account: str
    error: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class MigrationBatchState:
    """State of a migration batch"""
    batch_id: str
    started_at: str
    completed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # List of FailedAccount dicts
    pending: list[str] = field(default_factory=list)
    finished_at: Optional[str] = None


class MigrationState:
    """
    Manages migration state persistence using JSON.

    Usage:
        state = MigrationState()

        # Start a new batch
        batch_id = state.start_batch(["account1", "account2", "account3"])

        # Mark accounts as completed or failed
        state.mark_completed("account1")
        state.mark_failed("account2", "Connection timeout")

        # Resume after crash
        pending = state.get_pending()  # ["account3"]

        # Retry failed accounts
        failed = state.get_failed_accounts()  # ["account2"]
    """

    DEFAULT_STATE_FILE = Path("migration_state.json")

    def __init__(self, state_file: Optional[Path] = None):
        """
        Args:
            state_file: Path to state file. Defaults to migration_state.json
        """
        self.state_file = state_file or self.DEFAULT_STATE_FILE
        self._state: Optional[MigrationBatchState] = self._load()

    def _load(self) -> Optional[MigrationBatchState]:
        """Load state from file"""
        if not self.state_file.exists():
            return None

        try:
            data = json.loads(self.state_file.read_text(encoding='utf-8'))
            return MigrationBatchState(
                batch_id=data.get("batch_id", ""),
                started_at=data.get("started_at", ""),
                completed=data.get("completed", []),
                failed=data.get("failed", []),
                pending=data.get("pending", []),
                finished_at=data.get("finished_at")
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def _save(self) -> None:
        """
        Save state to file with atomic write and file locking.

        FIX-004: Uses temp file + rename for atomic write.
        File locking prevents race conditions in parallel migrations.
        """
        if self._state is None:
            return

        data = asdict(self._state)
        json_content = json.dumps(data, indent=2, ensure_ascii=False)
        temp_file = self.state_file.with_suffix('.tmp')

        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                try:
                    _lock_file(f)
                    locked = True
                except (IOError, OSError):
                    locked = False

                try:
                    f.write(json_content)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    if locked:
                        _unlock_file(f)

            os.replace(str(temp_file), str(self.state_file))

        except Exception:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise

    @property
    def has_active_batch(self) -> bool:
        """Check if there's an unfinished batch"""
        return (
            self._state is not None and
            self._state.finished_at is None and
            bool(self._state.pending)
        )

    @property
    def batch_id(self) -> Optional[str]:
        """Current batch ID"""
        return self._state.batch_id if self._state else None

    def start_batch(self, accounts: list[str]) -> str:
        """
        Start a new migration batch.

        Args:
            accounts: List of account names to migrate

        Returns:
            Batch ID (timestamp-based)
        """
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        self._state = MigrationBatchState(
            batch_id=batch_id,
            started_at=datetime.now().isoformat(),
            completed=[],
            failed=[],
            pending=accounts.copy()
        )

        self._save()
        return batch_id

    def mark_completed(self, account: str) -> None:
        """Mark an account as successfully migrated"""
        if self._state is None:
            return

        if account in self._state.pending:
            self._state.pending.remove(account)

        if account not in self._state.completed:
            self._state.completed.append(account)

        if not self._state.pending:
            self._state.finished_at = datetime.now().isoformat()

        self._save()

    def mark_failed(self, account: str, error: str) -> None:
        """Mark an account as failed"""
        if self._state is None:
            return

        if account in self._state.pending:
            self._state.pending.remove(account)

        # Add to failed list
        failed_record = {
            "account": account,
            "error": error,
            "timestamp": datetime.now().isoformat()
        }
        self._state.failed.append(failed_record)

        if not self._state.pending:
            self._state.finished_at = datetime.now().isoformat()

        self._save()

    def get_pending(self) -> list[str]:
        """Get list of accounts that haven't been processed yet"""
        if self._state is None:
            return []
        return self._state.pending.copy()

    def get_completed(self) -> list[str]:
        """Get list of successfully completed accounts"""
        if self._state is None:
            return []
        return self._state.completed.copy()

    def get_failed(self) -> list[dict]:
        """Get list of failed accounts with error details"""
        if self._state is None:
            return []
        return self._state.failed.copy()

    def get_failed_accounts(self) -> list[str]:
        """Get list of failed account names only"""
        if self._state is None:
            return []
        return [f["account"] for f in self._state.failed]

    def get_status(self) -> dict:
        """Get summary status of current batch"""
        if self._state is None:
            return {
                "has_batch": False,
                "batch_id": None,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0,
                "is_finished": True
            }

        total = (
            len(self._state.completed) +
            len(self._state.failed) +
            len(self._state.pending)
        )

        return {
            "has_batch": True,
            "batch_id": self._state.batch_id,
            "started_at": self._state.started_at,
            "finished_at": self._state.finished_at,
            "total": total,
            "completed": len(self._state.completed),
            "failed": len(self._state.failed),
            "pending": len(self._state.pending),
            "is_finished": self._state.finished_at is not None
        }

    def clear(self) -> None:
        """Clear current state (delete state file)"""
        if self.state_file.exists():
            self.state_file.unlink()
        self._state = None

    def format_status(self) -> str:
        """Format status as human-readable string"""
        status = self.get_status()

        if not status["has_batch"]:
            return "No active migration batch"

        lines = [
            f"Batch: {status['batch_id']}",
            f"Started: {status['started_at']}",
            f"Progress: {status['completed']}/{status['total']} completed",
            f"  - Completed: {status['completed']}",
            f"  - Failed: {status['failed']}",
            f"  - Pending: {status['pending']}",
        ]

        if status["is_finished"]:
            lines.append(f"Finished: {status['finished_at']}")
        else:
            lines.append("Status: IN PROGRESS")

        return "\n".join(lines)
