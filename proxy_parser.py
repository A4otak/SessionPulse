from typing import Optional, Tuple
import python_socks


def parse_proxy(proxy_str: str) -> Optional[Tuple]:
    """
    Преобразует строку прокси в кортеж для Telethon (через python-socks).
    Поддерживаемые форматы:
      - socks5://user:pass@ip:port
      - http://user:pass@ip:port
      - socks5://ip:port
      - ip:port:user:pass (по умолчанию socks5)
      - ip:port (по умолчанию socks5)
    """
    raw = proxy_str.strip()
    if not raw or raw.startswith("#"):
        return None

    proxy_type = python_socks.ProxyType.SOCKS5
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme in ("http", "https"):
            proxy_type = python_socks.ProxyType.HTTP
        elif scheme == "socks4":
            proxy_type = python_socks.ProxyType.SOCKS4
        else:
            proxy_type = python_socks.ProxyType.SOCKS5
    else:
        rest = raw

    host = ""
    port = 0
    username = None
    password = None

    if "@" in rest:
        auth_part, addr_part = rest.rsplit("@", 1)
        if ":" in auth_part:
            username, password = auth_part.split(":", 1)
        host, port_str = addr_part.split(":", 1)
        port = int(port_str)
    else:
        parts = rest.split(":")
        if len(parts) == 2:
            host, port = parts[0], int(parts[1])
        elif len(parts) == 4:
            host, port, username, password = parts[0], int(parts[1]), parts[2], parts[3]
        else:
            raise ValueError(f"Неверный формат прокси: {proxy_str}")

    if username and password:
        return (proxy_type, host, port, True, username, password)
    return (proxy_type, host, port)