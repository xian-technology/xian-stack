from __future__ import annotations

import ipaddress


def unbracket_host(host: str | None) -> str:
    value = (host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        return value[1:-1]
    return value


def display_host(host: str | None) -> str:
    value = unbracket_host(host)
    if value == "0.0.0.0":
        return "127.0.0.1"
    if value == "::":
        return "::1"
    return value


def url_host(host: str | None) -> str:
    value = display_host(host)
    if ":" in value and not (value.startswith("[") and value.endswith("]")):
        return f"[{value}]"
    return value


def http_url(host: str | None, port: int | str, path: str = "") -> str:
    return f"http://{url_host(host)}:{port}{path}"


def is_loopback_host(host: str | None) -> bool:
    value = unbracket_host(host).lower()
    if not value:
        return True
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False
