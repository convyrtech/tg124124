"""
Tests for proxy_relay module
"""
import pytest
from src.proxy_relay import ProxyConfig, ProxyRelay, needs_relay, find_free_port


class TestProxyConfig:
    """Tests for ProxyConfig parsing"""

    def test_parse_full_proxy_with_auth(self):
        """Test parsing full SOCKS5 proxy with auth"""
        config = ProxyConfig.parse("socks5:192.168.1.1:1080:user:pass123")

        assert config.protocol == "socks5"
        assert config.host == "192.168.1.1"
        assert config.port == 1080
        assert config.username == "user"
        assert config.password == "pass123"
        assert config.has_auth is True

    def test_parse_proxy_without_auth(self):
        """Test parsing proxy without credentials"""
        config = ProxyConfig.parse("socks5:proxy.example.com:1080")

        assert config.protocol == "socks5"
        assert config.host == "proxy.example.com"
        assert config.port == 1080
        assert config.username is None
        assert config.password is None
        assert config.has_auth is False

    def test_parse_http_proxy(self):
        """Test parsing HTTP proxy"""
        config = ProxyConfig.parse("http:proxy.com:8080:admin:secret")

        assert config.protocol == "http"
        assert config.host == "proxy.com"
        assert config.port == 8080
        assert config.has_auth is True

    def test_to_pproxy_uri_with_auth(self):
        """Test pproxy URI generation with auth"""
        config = ProxyConfig.parse("socks5:host:1080:user:pass")

        # pproxy uses # for credentials, not @
        assert config.to_pproxy_uri() == "socks5://host:1080#user:pass"

    def test_to_pproxy_uri_without_auth(self):
        """Test pproxy URI generation without auth"""
        config = ProxyConfig.parse("socks5:host:1080")

        assert config.to_pproxy_uri() == "socks5://host:1080"

    def test_invalid_format_raises(self):
        """Test that invalid format raises ValueError"""
        with pytest.raises(ValueError):
            ProxyConfig.parse("invalid")

        with pytest.raises(ValueError):
            ProxyConfig.parse("socks5:host")  # Missing port

        with pytest.raises(ValueError):
            ProxyConfig.parse("socks5:host:1080:user")  # Missing password


class TestNeedsRelay:
    """Tests for needs_relay function"""

    def test_socks5_with_auth_needs_relay(self):
        """SOCKS5 with auth requires relay"""
        assert needs_relay("socks5:host:1080:user:pass") is True

    def test_socks5_without_auth_no_relay(self):
        """SOCKS5 without auth doesn't need relay"""
        assert needs_relay("socks5:host:1080") is False

    def test_http_with_auth_no_relay(self):
        """HTTP proxy doesn't need relay (browser handles auth)"""
        # HTTP proxies support basic auth in browsers
        assert needs_relay("http:host:8080:user:pass") is False

    def test_empty_string_no_relay(self):
        """Empty string doesn't need relay"""
        assert needs_relay("") is False

    def test_none_no_relay(self):
        """None doesn't need relay"""
        assert needs_relay(None) is False


class TestFindFreePort:
    """Tests for find_free_port function"""

    def test_returns_valid_port(self):
        """Should return a valid port number"""
        port = find_free_port()

        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_returns_different_ports(self):
        """Should return different ports on multiple calls"""
        ports = [find_free_port() for _ in range(5)]
        # At least some should be different (not guaranteed all different
        # but very unlikely to get 5 same ports)
        assert len(set(ports)) >= 2


class TestProxyRelay:
    """Tests for ProxyRelay class (unit tests, no actual network)"""

    def test_init_parses_config(self):
        """Init should parse proxy config"""
        relay = ProxyRelay("socks5:host:1080:user:pass")

        assert relay.config.host == "host"
        assert relay.config.port == 1080
        assert relay.config.has_auth is True

    def test_local_url_none_before_start(self):
        """Local URL should be None before start"""
        relay = ProxyRelay("socks5:host:1080:user:pass")

        assert relay.local_url is None

    def test_browser_proxy_config_none_before_start(self):
        """Browser proxy config should be None before start"""
        relay = ProxyRelay("socks5:host:1080:user:pass")

        assert relay.browser_proxy_config is None

    def test_local_url_after_port_set(self):
        """Local URL should be correct after port is set"""
        relay = ProxyRelay("socks5:host:1080:user:pass")
        relay.local_port = 12345

        assert relay.local_url == "http://127.0.0.1:12345"

    def test_browser_proxy_config_after_port_set(self):
        """Browser proxy config should be correct after port is set"""
        relay = ProxyRelay("socks5:host:1080:user:pass")
        relay.local_port = 12345

        assert relay.browser_proxy_config == {"server": "http://127.0.0.1:12345"}

    def test_custom_local_host(self):
        """Should respect custom local host"""
        relay = ProxyRelay("socks5:host:1080:user:pass", local_host="0.0.0.0")
        relay.local_port = 8888

        assert relay.local_url == "http://0.0.0.0:8888"
