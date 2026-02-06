"""Tests for migration state persistence"""
import json
import pytest
from pathlib import Path

from src.migration_state import MigrationState, MigrationBatchState


class TestMigrationState:
    """Tests for MigrationState class"""

    def test_init_creates_empty_state(self, tmp_path):
        """State should be None when no file exists"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)
        assert state._state is None
        assert not state.has_active_batch

    def test_start_batch_creates_state(self, tmp_path):
        """start_batch should create state with all accounts in pending"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        accounts = ["account1", "account2", "account3"]
        batch_id = state.start_batch(accounts)

        assert batch_id is not None
        assert state.has_active_batch
        assert state.get_pending() == accounts
        assert state.get_completed() == []
        assert state.get_failed() == []

    def test_mark_completed_moves_to_completed(self, tmp_path):
        """mark_completed should move account from pending to completed"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1", "a2", "a3"])
        state.mark_completed("a1")

        assert "a1" not in state.get_pending()
        assert "a1" in state.get_completed()
        assert state.get_pending() == ["a2", "a3"]

    def test_mark_failed_moves_to_failed(self, tmp_path):
        """mark_failed should move account from pending to failed with error"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1", "a2"])
        state.mark_failed("a1", "Connection timeout")

        assert "a1" not in state.get_pending()
        assert state.get_failed_accounts() == ["a1"]
        failed = state.get_failed()
        assert len(failed) == 1
        assert failed[0]["account"] == "a1"
        assert failed[0]["error"] == "Connection timeout"

    def test_batch_finishes_when_pending_empty(self, tmp_path):
        """Batch should be marked finished when pending is empty"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1", "a2"])
        assert state.has_active_batch

        state.mark_completed("a1")
        assert state.has_active_batch  # Still has pending

        state.mark_failed("a2", "Error")
        assert not state.has_active_batch  # No more pending

        status = state.get_status()
        assert status["is_finished"] is True

    def test_persistence_to_file(self, tmp_path):
        """State should be saved to and loaded from file"""
        state_file = tmp_path / "state.json"

        # Create and populate state
        state1 = MigrationState(state_file)
        state1.start_batch(["a1", "a2", "a3"])
        state1.mark_completed("a1")
        state1.mark_failed("a2", "Error")

        # Load state from file
        state2 = MigrationState(state_file)
        assert state2.get_pending() == ["a3"]
        assert state2.get_completed() == ["a1"]
        assert state2.get_failed_accounts() == ["a2"]

    def test_get_status_no_batch(self, tmp_path):
        """get_status should return empty status when no batch"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        status = state.get_status()
        assert status["has_batch"] is False
        assert status["total"] == 0

    def test_get_status_with_batch(self, tmp_path):
        """get_status should return correct counts"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1", "a2", "a3", "a4"])
        state.mark_completed("a1")
        state.mark_failed("a2", "Error")

        status = state.get_status()
        assert status["has_batch"] is True
        assert status["total"] == 4
        assert status["completed"] == 1
        assert status["failed"] == 1
        assert status["pending"] == 2

    def test_clear_removes_state_file(self, tmp_path):
        """clear should remove state file"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1"])
        assert state_file.exists()

        state.clear()
        assert not state_file.exists()
        assert state._state is None

    def test_corrupted_file_returns_none(self, tmp_path):
        """Corrupted state file should result in None state"""
        state_file = tmp_path / "state.json"
        state_file.write_text("invalid json {{{")

        state = MigrationState(state_file)
        assert state._state is None

    def test_format_status_no_batch(self, tmp_path):
        """format_status should work with no batch"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        formatted = state.format_status()
        assert "No active migration batch" in formatted

    def test_format_status_with_batch(self, tmp_path):
        """format_status should show batch info"""
        state_file = tmp_path / "state.json"
        state = MigrationState(state_file)

        state.start_batch(["a1", "a2"])
        state.mark_completed("a1")

        formatted = state.format_status()
        assert "Batch:" in formatted
        assert "1/2" in formatted or "Completed: 1" in formatted
