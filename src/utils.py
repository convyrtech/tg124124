"""
Utility functions for TG Web Auth.

Общие функции используемые в разных модулях.
"""

import re
from typing import Any


def parse_proxy_for_camoufox(proxy_str: str) -> dict[str, Any]:
    """
    Парсит прокси в формат Camoufox/Playwright.

    Args:
        proxy_str: Прокси в формате 'socks5:host:port:user:pass' или 'socks5:host:port'

    Returns:
        Dict с ключами: server, username (опц.), password (опц.)

    Raises:
        ValueError: Если формат прокси невалидный

    Examples:
        >>> parse_proxy_for_camoufox("socks5:proxy.example.com:1080:user:pass")
        {'server': 'socks5://proxy.example.com:1080', 'username': 'user', 'password': 'pass'}
    """
    if not proxy_str:
        raise ValueError("Proxy string cannot be empty")

    parts = proxy_str.split(":", 4)
    if len(parts) == 5:
        proto, host, port, user, pwd = parts
        return {
            "server": f"{proto}://{host}:{port}",
            "username": user,
            "password": pwd,
        }
    elif len(parts) == 3:
        proto, host, port = parts
        return {"server": f"{proto}://{host}:{port}"}

    raise ValueError(f"Invalid proxy format (expected 'protocol:host:port[:user:pass]', got {len(parts)} parts)")


def parse_proxy_for_telethon(proxy_str: str) -> tuple | None:
    """
    Конвертирует прокси в формат Telethon.

    Args:
        proxy_str: Прокси в формате 'socks5:host:port:user:pass'

    Returns:
        Tuple (proxy_type, host, port, rdns, user, pass) или None

    Examples:
        >>> parse_proxy_for_telethon("socks5:proxy.example.com:1080:user:pass")
        (2, 'proxy.example.com', 1080, True, 'user', 'pass')
    """
    if not proxy_str:
        return None

    try:
        import socks
    except ImportError as e:
        raise ImportError("PySocks not installed. Run: pip install PySocks") from e

    parts = proxy_str.split(":", 4)
    try:
        if len(parts) == 5:
            proto, host, port, user, pwd = parts
            proxy_type = socks.SOCKS5 if "socks5" in proto.lower() else socks.HTTP
            return (proxy_type, host, int(port), True, user, pwd)
        elif len(parts) == 3:
            proto, host, port = parts
            proxy_type = socks.SOCKS5 if "socks5" in proto.lower() else socks.HTTP
            return (proxy_type, host, int(port))
    except (ValueError, TypeError):
        return None

    return None


def mask_proxy_credentials(proxy_str: str) -> str:
    """
    Маскирует credentials в прокси строке для безопасного логирования.

    Args:
        proxy_str: Прокси строка

    Returns:
        Строка с замаскированными credentials

    Examples:
        >>> mask_proxy_credentials("socks5:proxy.com:1080:user:secretpass")
        'socks5:proxy.com:1080:***:***'
    """
    if not proxy_str:
        return ""

    # Try colon-delimited format first: proto:host:port:user:pass
    parts = proxy_str.split(":", 4)
    if len(parts) == 5:
        proto, host, port, _, _ = parts
        return f"{proto}:{host}:{port}:***:***"

    # Handle user:pass@host:port format
    if "@" in proxy_str:
        at_idx = proxy_str.index("@")
        return "***:***@" + proxy_str[at_idx + 1 :]

    return proxy_str


# Regex patterns for credential-like data in error messages
_PROXY_PATTERN = re.compile(
    r"(socks[45]?|https?|http)([:/]+)([a-zA-Z0-9._-]+:\d+)(:\S+:\S+)", re.IGNORECASE
)
_CREDENTIAL_URI_PATTERN = re.compile(
    r"://([^:@]+):([^@]+)@",
)
# pproxy hash format: socks5://host:port#user:pass
_PPROXY_HASH_PATTERN = re.compile(
    r"((?:socks[45]?|https?|http)://[a-zA-Z0-9._-]+:\d+)#(\S+:\S+)", re.IGNORECASE
)
# Bare user:pass@host (without :// prefix)
_BARE_CRED_AT_PATTERN = re.compile(
    r"(\S+):(\S+)@([a-zA-Z0-9._-]+(?::\d+)?)",
)
_PHONE_PATTERN = re.compile(r"\+\d{10,14}\b")
# Russian phone numbers without + prefix (79991234567)
_PHONE_NO_PLUS_PATTERN = re.compile(r"\b7[0-9]{10}\b")
# api_hash in tracebacks: api_hash='b18441a1ff607e10a989891a5462e627'
_API_HASH_PATTERN = re.compile(r"api_hash\s*[=:]\s*['\"]?[0-9a-fA-F]{32}['\"]?", re.IGNORECASE)


def sanitize_error(error_text: str) -> str:
    """
    Remove credentials, proxy passwords, phone numbers from error text.

    Safe for logging, DB storage, crash files, diagnostics.

    Args:
        error_text: Raw error string that may contain sensitive data

    Returns:
        Sanitized string with credentials masked
    """
    if not error_text:
        return ""

    text = str(error_text)
    # Mask pproxy hash format first (protocol://host:port#user:pass)
    text = _PPROXY_HASH_PATTERN.sub(r"\1#***:***", text)
    # Mask proxy credentials in protocol:host:port:user:pass format
    text = _PROXY_PATTERN.sub(r"\1\2\3:***:***", text)
    # Mask credentials in URI format (user:pass@host)
    text = _CREDENTIAL_URI_PATTERN.sub(r"://***:***@", text)
    # Mask bare user:pass@host (without :// prefix)
    text = _BARE_CRED_AT_PATTERN.sub(r"***:***@\3", text)
    # Mask api_hash (32-char hex in tracebacks)
    text = _API_HASH_PATTERN.sub("api_hash=[MASKED]", text)
    # Mask phone numbers
    text = _PHONE_PATTERN.sub("[phone]", text)
    text = _PHONE_NO_PLUS_PATTERN.sub("[phone]", text)
    return text
