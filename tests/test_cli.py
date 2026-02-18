"""
CLI smoke tests using Click's CliRunner.

Tests that CLI commands are properly wired, accept correct arguments,
and produce expected output without crashing. Does NOT test business
logic (that's covered by unit tests of the underlying modules).
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from click.testing import CliRunner

from src.cli import cli


@pytest.fixture
def runner():
    """Click test runner."""
    return CliRunner()


class TestCLIHelp:
    """Verify help output for all commands."""

    def test_cli_main_help(self, runner):
        """Main CLI --help doesn't crash and lists commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "migrate" in result.output
        assert "list" in result.output
        assert "fragment" in result.output

    def test_migrate_help(self, runner):
        """migrate --help shows expected options."""
        result = runner.invoke(cli, ["migrate", "--help"])
        assert result.exit_code == 0
        assert "--account" in result.output
        assert "--all" in result.output

    def test_fragment_help(self, runner):
        """fragment --help shows expected options."""
        result = runner.invoke(cli, ["fragment", "--help"])
        assert result.exit_code == 0
        assert "--account" in result.output

    def test_check_help(self, runner):
        """check --help shows expected options."""
        result = runner.invoke(cli, ["check", "--help"])
        assert result.exit_code == 0
        assert "--proxy" in result.output or "-p" in result.output

    def test_check_proxies_help(self, runner):
        """check-proxies --help shows options."""
        result = runner.invoke(cli, ["check-proxies", "--help"])
        assert result.exit_code == 0

    def test_proxy_refresh_help(self, runner):
        """proxy-refresh --help shows expected options."""
        result = runner.invoke(cli, ["proxy-refresh", "--help"])
        assert result.exit_code == 0
        assert "--file" in result.output or "-f" in result.output

    def test_health_help(self, runner):
        """health --help shows expected options."""
        result = runner.invoke(cli, ["health", "--help"])
        assert result.exit_code == 0

    def test_open_help(self, runner):
        """open --help shows expected options."""
        result = runner.invoke(cli, ["open", "--help"])
        assert result.exit_code == 0


class TestCLIInit:
    """Test init command."""

    def test_init_creates_directories(self, runner, tmp_path):
        """init command creates accounts/ and profiles/ directories."""
        with patch("src.cli.ACCOUNTS_DIR", tmp_path / "accounts"), \
             patch("src.cli.PROFILES_DIR", tmp_path / "profiles"):
            result = runner.invoke(cli, ["init"])
            assert result.exit_code == 0
            assert "Созданы директории" in result.output
            assert (tmp_path / "accounts").exists()
            assert (tmp_path / "profiles").exists()


class TestCLIList:
    """Test list command."""

    def test_list_no_accounts(self, runner, tmp_path):
        """list command with no accounts shows empty state."""
        with patch("src.cli.ACCOUNTS_DIR", tmp_path / "accounts"), \
             patch("src.cli.PROFILES_DIR", tmp_path / "profiles"), \
             patch("src.cli.find_account_dirs", return_value=[]), \
             patch("src.browser_manager.BrowserManager") as mock_bm:
            mock_bm.return_value.list_profiles.return_value = []
            (tmp_path / "accounts").mkdir()
            result = runner.invoke(cli, ["list"])
            assert result.exit_code == 0
            assert "нет аккаунтов" in result.output.lower() or "АККАУНТЫ" in result.output
