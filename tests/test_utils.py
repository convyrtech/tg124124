"""
Tests for utils module.
"""
import pytest
from src.utils import (
    parse_proxy_for_camoufox,
    parse_proxy_for_telethon,
    mask_proxy_credentials,
)


class TestParseProxyForCamoufox:
    """Tests for parse_proxy_for_camoufox function."""

    def test_full_proxy_with_auth(self):
        """Test parsing proxy with authentication."""
        result = parse_proxy_for_camoufox("socks5:proxy.example.com:1080:user:pass123")
        assert result == {
            "server": "socks5://proxy.example.com:1080",
            "username": "user",
            "password": "pass123",
        }

    def test_proxy_without_auth(self):
        """Test parsing proxy without authentication."""
        result = parse_proxy_for_camoufox("socks5:proxy.example.com:1080")
        assert result == {"server": "socks5://proxy.example.com:1080"}

    def test_http_proxy(self):
        """Test parsing HTTP proxy."""
        result = parse_proxy_for_camoufox("http:proxy.example.com:8080:user:pass")
        assert result["server"] == "http://proxy.example.com:8080"

    def test_invalid_format_too_few_parts(self):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid proxy format"):
            parse_proxy_for_camoufox("socks5:proxy.com")

    def test_extra_colons_in_password(self):
        """Extra colons are part of password (maxsplit=4)."""
        result = parse_proxy_for_camoufox("socks5:host:1080:user:pass:extra")
        assert result["password"] == "pass:extra"

    def test_empty_string(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_proxy_for_camoufox("")

    def test_special_characters_in_password(self):
        """Test proxy with special characters in password."""
        # Password with colons now works (maxsplit=4)
        result = parse_proxy_for_camoufox("socks5:host:1080:user:p@ss:w0rd")
        assert result["password"] == "p@ss:w0rd"
        assert result["username"] == "user"
        assert result["server"] == "socks5://host:1080"

        # @ and other chars without colon work fine
        result = parse_proxy_for_camoufox("socks5:host:1080:user:p@ssw0rd!")
        assert result["password"] == "p@ssw0rd!"


class TestParseProxyForTelethon:
    """Tests for parse_proxy_for_telethon function."""

    def test_socks5_proxy_with_auth(self):
        """Test parsing SOCKS5 proxy with authentication."""
        result = parse_proxy_for_telethon("socks5:proxy.example.com:1080:user:pass")
        assert result is not None
        assert len(result) == 6
        # result[0] is socks.SOCKS5 (value 2)
        assert result[1] == "proxy.example.com"
        assert result[2] == 1080
        assert result[3] is True  # rdns
        assert result[4] == "user"
        assert result[5] == "pass"

    def test_socks5_proxy_without_auth(self):
        """Test parsing SOCKS5 proxy without authentication."""
        result = parse_proxy_for_telethon("socks5:proxy.example.com:1080")
        assert result is not None
        assert len(result) == 3
        assert result[1] == "proxy.example.com"
        assert result[2] == 1080

    def test_none_input(self):
        """Test that None input returns None."""
        result = parse_proxy_for_telethon(None)
        assert result is None

    def test_empty_string(self):
        """Test that empty string returns None."""
        result = parse_proxy_for_telethon("")
        assert result is None

    def test_invalid_format(self):
        """Test that invalid format returns None."""
        result = parse_proxy_for_telethon("invalid")
        assert result is None


class TestMaskProxyCredentials:
    """Tests for mask_proxy_credentials function."""

    def test_mask_full_proxy(self):
        """Test masking proxy with credentials."""
        result = mask_proxy_credentials("socks5:proxy.com:1080:myuser:secretpass")
        assert result == "socks5:proxy.com:1080:***:***"
        assert "myuser" not in result
        assert "secretpass" not in result

    def test_proxy_without_auth(self):
        """Test that proxy without auth is returned as-is."""
        result = mask_proxy_credentials("socks5:proxy.com:1080")
        assert result == "socks5:proxy.com:1080"

    def test_empty_string(self):
        """Test empty string returns empty."""
        result = mask_proxy_credentials("")
        assert result == ""

    def test_none_like(self):
        """Test None-like input."""
        result = mask_proxy_credentials(None)
        assert result == ""

    def test_mask_password_with_colons(self):
        """Password with colons should still be masked."""
        result = mask_proxy_credentials("socks5:proxy.com:1080:user:pass:w0rd")
        assert result == "socks5:proxy.com:1080:***:***"
        assert "pass:w0rd" not in result

    def test_mask_password_with_at_sign(self):
        """Password with @ in colon format is masked via colon branch."""
        result = mask_proxy_credentials("socks5:proxy.com:1080:user:p@ss")
        assert result == "socks5:proxy.com:1080:***:***"


class TestPasswordWithColons:
    """Cross-function tests: passwords containing colons must work everywhere."""

    def test_camoufox_parser(self):
        result = parse_proxy_for_camoufox("socks5:host:1080:user:a:b:c")
        assert result["password"] == "a:b:c"
        assert result["username"] == "user"

    def test_telethon_parser(self):
        result = parse_proxy_for_telethon("socks5:host:1080:user:a:b:c")
        assert result is not None
        assert result[4] == "user"
        assert result[5] == "a:b:c"
