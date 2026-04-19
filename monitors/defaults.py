"""
Shared default values for monitors.
Read from config.stealth at runtime, with sensible fallbacks.
"""

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
