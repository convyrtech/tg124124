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

    def test_invalid_format_too_many_parts(self):
        """Test that too many parts raises ValueError."""
        with pytest.raises(ValueError, match="Invalid proxy format"):
            parse_proxy_for_camoufox("socks5:host:port:user:pass:extra")

    def test_empty_string(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            parse_proxy_for_camoufox("")

    def test_special_characters_in_password(self):
        """Test proxy with special characters in password.

        Known limitation: Passwords with colons break parsing.
        This test documents the behavior.
        """
        # Password with colon breaks the parser (6 parts instead of 5)
        with pytest.raises(ValueError, match="Invalid proxy format"):
            parse_proxy_for_camoufox("socks5:host:1080:user:p@ss:w0rd")

        # But @ and other chars without colon work fine
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
