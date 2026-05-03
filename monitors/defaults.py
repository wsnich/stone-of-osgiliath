"""
Shared default values for monitors.
Read from config.stealth at runtime, with sensible fallbacks.
"""

import random
import threading
import time

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_PAGE_TIMEOUT = 60_000      # ms — browser page load (60s for slow residential proxies)
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

_bad_proxies: dict[str, float] = {}   # proxy_url -> time.monotonic() when marked bad
_proxy_fail_count: dict[str, int] = {}   # proxy_url -> consecutive failure count
_bad_lock = threading.Lock()
_BAD_PROXY_COOLDOWN = 120         # seconds before a bad proxy is eligible again
_FAIL_THRESHOLD     = 2           # consecutive failures before marking bad


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
    Skips proxies marked bad within the cooldown window.  Returns None if
    no proxies configured or all are still in cooldown.
    """
    if not stealth_cfg:
        return None
    pool = _parse_proxy_list(stealth_cfg.get("proxy"))
    if not pool:
        return None
    now = time.monotonic()
    with _bad_lock:
        available = [p for p in pool if now - _bad_proxies.get(p, 0) >= _BAD_PROXY_COOLDOWN]
    if not available:
        # All proxies in cooldown — return None so callers skip the proxy
        return None
    return random.choice(available)


def mark_proxy_bad(proxy_url: str) -> None:
    """Record a failure. Only puts the proxy in cooldown after _FAIL_THRESHOLD
    consecutive failures — single transient failures don't penalize it."""
    if not proxy_url:
        return
    with _bad_lock:
        _proxy_fail_count[proxy_url] = _proxy_fail_count.get(proxy_url, 0) + 1
        if _proxy_fail_count[proxy_url] >= _FAIL_THRESHOLD:
            _bad_proxies[proxy_url] = time.monotonic()


def mark_proxy_good(proxy_url: str) -> None:
    """Reset the failure count after a successful use."""
    if not proxy_url:
        return
    with _bad_lock:
        _proxy_fail_count.pop(proxy_url, None)
        _bad_proxies.pop(proxy_url, None)


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
