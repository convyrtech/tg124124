"""
Utility functions for TG Web Auth.

Общие функции используемые в разных модулях.
"""
from typing import Dict, Any, Optional, Tuple


def parse_proxy_for_camoufox(proxy_str: str) -> Dict[str, Any]:
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

    parts = proxy_str.split(":")
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

    raise ValueError(
        f"Invalid proxy format: '{proxy_str}'. "
        f"Expected: 'protocol:host:port:user:pass' or 'protocol:host:port'"
    )


def parse_proxy_for_telethon(proxy_str: str) -> Optional[Tuple]:
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
    except ImportError:
        raise ImportError("PySocks not installed. Run: pip install PySocks")

    parts = proxy_str.split(":")
    if len(parts) == 5:
        proto, host, port, user, pwd = parts
        proxy_type = socks.SOCKS5 if 'socks5' in proto.lower() else socks.HTTP
        return (proxy_type, host, int(port), True, user, pwd)
    elif len(parts) == 3:
        proto, host, port = parts
        proxy_type = socks.SOCKS5 if 'socks5' in proto.lower() else socks.HTTP
        return (proxy_type, host, int(port))

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

    parts = proxy_str.split(":")
    if len(parts) == 5:
        proto, host, port, _, _ = parts
        return f"{proto}:{host}:{port}:***:***"
    return proxy_str
