"""
Helpers for validating telephony callback URLs.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlparse


def is_public_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
        return False

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True

    return ip.is_global


def validate_public_callback_url(url: str, provider_name: str = "provider") -> str:
    normalized = str(url or "").strip()
    if not is_public_callback_url(normalized):
        raise ValueError(
            f"BASE_URL must be a public http(s) URL reachable by {provider_name} callbacks"
        )
    return normalized


def build_public_callback_url(base_url: str, path: str, provider_name: str = "provider") -> str:
    normalized_base = str(base_url or "").strip()
    callback_url = urljoin(f"{normalized_base.rstrip('/')}/", str(path or "").lstrip("/"))
    return validate_public_callback_url(callback_url, provider_name=provider_name)
