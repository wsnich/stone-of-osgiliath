"""
Shared default values for monitors.
Read from config.stealth at runtime, with sensible fallbacks.
"""

import random
import threading

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_PAGE_TIMEOUT = 30_000      # ms — browser page load
DEFAULT_NETWORK_TIMEOUT = 15       # seconds — HTTP requests


def get_user_agent(stealth_cfg: dict | None = None) -> str:
    if stealth_cfg and stealth_cfg.get("user_agent"):
        return stealth_cfg["user_agent"]
    return DEFAULT_USER_AGENT


def get_page_timeout(stealth_cfg: dict | None = None) -> int:
    if stealth_cfg and stealth_cfg.get("page_timeout_ms"):
        return int(stealth_cfg["page_timeout_ms"])
    return DEFAULT_PAGE_TIMEOUT


def get_network_timeout(stealth_cfg: dict | None = None) -> int:
    if stealth_cfg and stealth_cfg.get("network_timeout_seconds"):
        return int(stealth_cfg["network_timeout_seconds"])
    return DEFAULT_NETWORK_TIMEOUT


def get_browser_channel(stealth_cfg: dict | None = None) -> str | None:
    """Return preferred browser channel or None for auto-detect."""
    if stealth_cfg and stealth_cfg.get("browser_channel"):
        return stealth_cfg["browser_channel"]
    return None


# ── Proxy rotation ────────────────────────────────────────────────────────────
# config.stealth.proxy accepts either:
#   - a single URL string:  "socks5://host:1080"
#   - a list of URLs:       ["socks5://h1:1080", "http://user:pass@h2:3128"]
#   - a newline/comma-separated string (pasted from a proxy list)
#
# Rotation is random per call so different concurrent product checks use
# different IPs.  Failed proxies can be marked bad via mark_proxy_bad() and
# won't be selected again until the pool is reset.

_bad_proxies: set[str] = set()
_bad_lock = threading.Lock()


def _parse_proxy_list(raw) -> list[str]:
    """Normalise raw config value to a deduplicated list of proxy URL strings.

    Accepts:
      - Standard URL:          http://user:pass@host:port
      - proxy-cheap export:    host:port:user:pass
      - List of either format
      - Newline/comma-separated string of either format
    """
    if not raw:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        import re
        entries = re.split(r"[\n,]+", str(raw))

    result = []
    seen: set[str] = set()
    for e in entries:
        e = e.strip()
        if not e:
            continue
        # Convert host:port:user:pass → scheme://user:pass@host:port
        # Detect SOCKS5 by common ports (9595, 1080) otherwise assume HTTP
        _SOCKS5_PORTS = {"9595", "1080", "1081", "9050"}
        if not e.startswith(("http://", "https://", "socks5://", "socks4://")):
            parts = e.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                scheme = "socks5" if port in _SOCKS5_PORTS else "http"
                e = f"{scheme}://{user}:{password}@{host}:{port}"
        if e not in seen:
            seen.add(e)
            result.append(e)
    return result


def get_proxy(stealth_cfg: dict | None = None) -> str | None:
    """Return one proxy URL chosen at random from the configured pool.
    Skips proxies previously marked bad.  Returns None if no proxies configured.
    """
    if not stealth_cfg:
        return None
    pool = _parse_proxy_list(stealth_cfg.get("proxy"))
    if not pool:
        return None
    with _bad_lock:
        available = [p for p in pool if p not in _bad_proxies]
    if not available:
        # All marked bad — reset and try the full pool again
        with _bad_lock:
            _bad_proxies.clear()
        available = pool
    return random.choice(available)


def mark_proxy_bad(proxy_url: str) -> None:
    """Flag a proxy as failed so it won't be picked until the pool resets."""
    if proxy_url:
        with _bad_lock:
            _bad_proxies.add(proxy_url)


def playwright_proxy(stealth_cfg: dict | None = None) -> dict | None:
    """Return a Playwright-compatible proxy dict or None."""
    url = get_proxy(stealth_cfg)
    if not url:
        return None
    from urllib.parse import urlparse
    p = urlparse(url)
    # Playwright always requires credentials as separate fields regardless of scheme
    proxy: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy
