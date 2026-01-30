"""
Integration tests for TG Web Auth.

These tests verify the full flow works correctly without making actual
network requests or browser launches.
"""
import pytest
import json
from pathlib import Path

from src.telegram_auth import AccountConfig, TelegramAuth
from src.browser_manager import BrowserManager, BrowserProfile
from src.utils import parse_proxy_for_camoufox, parse_proxy_for_telethon


class TestAccountLoadingIntegration:
    """Test complete account loading flow."""

    def test_load_account_and_validate_all_fields(self, sample_account_dir):
        """
        Integration test: Load account and verify all fields are correctly parsed.
        """
        # Load account
        config = AccountConfig.load(sample_account_dir)

        # Verify all fields
        assert config.name == "Test Account"
        assert config.api_id == 2040
        assert config.api_hash == "b18441a1ff607e10a989891a5462e627"
        assert config.proxy == "socks5:proxy.example.com:1080:testuser:testpass"
        assert config.session_path.exists()
        assert config.session_path.suffix == ".session"

        # Verify proxy can be parsed for both Camoufox and Telethon
        camoufox_proxy = parse_proxy_for_camoufox(config.proxy)
        assert camoufox_proxy["server"] == "socks5://proxy.example.com:1080"
        assert camoufox_proxy["username"] == "testuser"

        telethon_proxy = parse_proxy_for_telethon(config.proxy)
        assert telethon_proxy is not None
        assert telethon_proxy[1] == "proxy.example.com"
        assert telethon_proxy[2] == 1080

    def test_load_multiple_accounts(self, accounts_dir):
        """
        Integration test: Load multiple accounts and verify each.
        """
        loaded_accounts = []

        for account_path in accounts_dir.iterdir():
            if account_path.is_dir():
                config = AccountConfig.load(account_path)
                loaded_accounts.append(config)

        assert len(loaded_accounts) == 3

        # Verify each account has unique data
        names = [a.name for a in loaded_accounts]
        assert "Account 1" in names
        assert "Account 2" in names
        assert "Account 3" in names

        # Verify api_ids are different
        api_ids = [a.api_id for a in loaded_accounts]
        assert len(set(api_ids)) == 3


class TestBrowserManagerIntegration:
    """Test BrowserManager profile management."""

    def test_create_and_list_profiles(self, tmp_path):
        """
        Integration test: Create profiles and list them.
        """
        manager = BrowserManager(profiles_dir=tmp_path / "profiles")

        # Create profiles
        profile1 = manager.get_profile("account_1", "socks5:h:p:u:p")
        profile2 = manager.get_profile("account_2", "socks5:h:p:u:p")

        # Simulate that profiles were used (create browser_data dirs)
        profile1.browser_data_path.mkdir(parents=True)
        profile2.browser_data_path.mkdir(parents=True)

        # Save configs
        manager._save_profile_config(profile1)
        manager._save_profile_config(profile2)

        # List profiles
        profiles = manager.list_profiles()

        assert len(profiles) == 2
        names = [p.name for p in profiles]
        assert "account_1" in names
        assert "account_2" in names

    def test_profile_persistence(self, tmp_path):
        """
        Integration test: Verify profile config persists.
        """
        profiles_dir = tmp_path / "profiles"

        # Create manager and profile
        manager1 = BrowserManager(profiles_dir=profiles_dir)
        profile = manager1.get_profile("persistent", "socks5:host:1080:user:secret")
        profile.browser_data_path.mkdir(parents=True)
        manager1._save_profile_config(profile)

        # Create new manager instance (simulating restart)
        manager2 = BrowserManager(profiles_dir=profiles_dir)
        profiles = manager2.list_profiles()

        assert len(profiles) == 1
        assert profiles[0].name == "persistent"
        assert profiles[0].proxy == "socks5:host:1080:user:secret"


class TestTelegramAuthIntegration:
    """Test TelegramAuth initialization and config."""

    def test_telegram_auth_setup(self, sample_account_dir, tmp_path):
        """
        Integration test: Set up TelegramAuth with account and browser manager.
        """
        # Load account
        account = AccountConfig.load(sample_account_dir)

        # Create browser manager
        browser_manager = BrowserManager(profiles_dir=tmp_path / "profiles")

        # Create TelegramAuth
        auth = TelegramAuth(account, browser_manager)

        # Verify setup
        assert auth.account.name == "Test Account"
        assert auth.browser_manager == browser_manager

        # Verify profile would be created correctly
        profile = browser_manager.get_profile(account.name, account.proxy)
        assert profile.name == "Test Account"
        assert profile.proxy == account.proxy


class TestEndToEndValidation:
    """
    End-to-end validation tests.

    These verify the complete data flow from account files to
    auth configuration without network operations.
    """

    def test_complete_validation_flow(self, sample_account_dir, tmp_path):
        """
        Integration test: Complete validation of account → auth setup.

        This test verifies:
        1. Account files can be loaded
        2. Proxy format is valid for both systems
        3. BrowserManager can create profile
        4. TelegramAuth can be initialized
        """
        # Step 1: Load and validate account
        account = AccountConfig.load(sample_account_dir)
        assert account.api_id > 0
        assert len(account.api_hash) > 0
        assert account.session_path.exists()

        # Step 2: Validate proxy formats
        if account.proxy:
            # Must work for Camoufox
            camoufox_proxy = parse_proxy_for_camoufox(account.proxy)
            assert "server" in camoufox_proxy

            # Must work for Telethon
            telethon_proxy = parse_proxy_for_telethon(account.proxy)
            assert telethon_proxy is not None

        # Step 3: Create browser profile
        browser_manager = BrowserManager(profiles_dir=tmp_path / "profiles")
        profile = browser_manager.get_profile(account.name, account.proxy)

        # Verify profile paths are valid
        assert profile.name == account.name
        assert profile.path.parent == tmp_path / "profiles"

        # Step 4: Initialize TelegramAuth
        auth = TelegramAuth(account, browser_manager)

        # Final validation
        assert auth.account == account
        assert auth.TELEGRAM_WEB_URL.startswith("https://")

        print(f"✓ Account: {account.name}")
        print(f"✓ API ID: {account.api_id}")
        print(f"✓ Session: {account.session_path.name}")
        print(f"✓ Proxy: {'configured' if account.proxy else 'none'}")
        print(f"✓ Profile path: {profile.path}")

    def test_real_accounts_structure(self):
        """
        Integration test: Validate real accounts directory structure.

        This test checks the actual accounts/ directory if it exists.
        """
        accounts_path = Path("accounts")
        if not accounts_path.exists():
            pytest.skip("No accounts/ directory found")

        errors = []
        loaded = 0

        for account_dir in accounts_path.rglob("*.session"):
            parent = account_dir.parent
            try:
                config = AccountConfig.load(parent)
                loaded += 1

                # Validate proxy if present
                if config.proxy:
                    try:
                        parse_proxy_for_camoufox(config.proxy)
                        parse_proxy_for_telethon(config.proxy)
                    except Exception as e:
                        errors.append(f"{config.name}: invalid proxy - {e}")

            except Exception as e:
                errors.append(f"{parent.name}: {e}")

        print(f"\nLoaded {loaded} accounts")
        if errors:
            print(f"Errors: {len(errors)}")
            for err in errors:
                print(f"  - {err}")

        # Allow some errors but not all
        assert loaded > 0 or not accounts_path.exists(), "No accounts could be loaded"
