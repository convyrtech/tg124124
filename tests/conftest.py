"""
Pytest configuration and shared fixtures.
"""
import pytest
import json
from pathlib import Path


@pytest.fixture
def sample_account_dir(tmp_path):
    """
    Create a complete sample account directory structure.

    Returns the path to the account directory.
    """
    account_dir = tmp_path / "sample_account"
    account_dir.mkdir()

    # Session file (empty, just for structure)
    (account_dir / "session.session").touch()

    # API config
    api_config = {
        "api_id": 2040,
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "device_model": "Desktop",
        "system_version": "Windows 10"
    }
    with open(account_dir / "api.json", 'w', encoding='utf-8') as f:
        json.dump(api_config, f, indent=2)

    # Account config with proxy
    account_config = {
        "Name": "Test Account",
        "Proxy": "socks5:proxy.example.com:1080:testuser:testpass"
    }
    with open(account_dir / "___config.json", 'w', encoding='utf-8') as f:
        json.dump(account_config, f, indent=2)

    return account_dir


@pytest.fixture
def sample_profile_dir(tmp_path):
    """
    Create a sample browser profile directory structure.

    Returns the path to the profile directory.
    """
    profile_dir = tmp_path / "profiles" / "sample_profile"
    (profile_dir / "browser_data").mkdir(parents=True)

    # Profile config
    profile_config = {
        "name": "sample_profile",
        "proxy": "socks5:proxy.example.com:1080:user:pass"
    }
    with open(profile_dir / "profile_config.json", 'w', encoding='utf-8') as f:
        json.dump(profile_config, f, indent=2)

    # Empty storage state
    storage_state = {"cookies": [], "origins": []}
    with open(profile_dir / "storage_state.json", 'w', encoding='utf-8') as f:
        json.dump(storage_state, f, indent=2)

    return profile_dir


@pytest.fixture
def accounts_dir(tmp_path):
    """
    Create an accounts directory with multiple accounts.
    """
    accounts = tmp_path / "accounts"
    accounts.mkdir()

    # Create 3 sample accounts
    for i in range(1, 4):
        account_dir = accounts / f"account_{i}"
        account_dir.mkdir()

        (account_dir / "session.session").touch()

        with open(account_dir / "api.json", 'w') as f:
            json.dump({"api_id": 1000 + i, "api_hash": f"hash{i}"}, f)

        with open(account_dir / "___config.json", 'w') as f:
            json.dump({
                "Name": f"Account {i}",
                "Proxy": f"socks5:proxy{i}.com:1080:user{i}:pass{i}"
            }, f)

    return accounts
