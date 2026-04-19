"""
Best Buy product price & availability monitor.

Best Buy's Akamai aggressively blocks product detail pages (HTTP/2 stream
resets) from ALL HTTP clients and headless browsers.  However, their
internal pricing API is accessible via safari TLS impersonation:

  GET /api/3.0/priceBlocks?skus={sku}   — price + availability JSON
  GET /pricing/v1/price/item?skuId={sku} — requires X-CLIENT-ID header

This monitor uses the priceBlocks API as primary, with the pricing v1
API as fallback.  &intl=nosplash is appended to any page-level requests
to bypass the country selection splash screen.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("bestbuy")

from monitors.walmart_monitor import ProductResult

_UA_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

_API_HEADERS = {
    "User-Agent":      _UA_SAFARI,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.bestbuy.com/",
    "Origin":          "https://www.bestbuy.com",
}

_CAPTCHA_COOLDOWN = 900
_PROJECT_ROOT = Path(__file__).parent.parent


class BestBuyMonitor:

    def __init__(self):
        self._proxy_idx = 0
        self._cooldown_until: float = 0.0
        self._proxies: list[str] = []
        self._proxies_loaded = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def check_product(self, product: dict, stealth_cfg: dict | None = None) -> ProductResult:
        url  = product["url"]
        name = product["name"]
        stealth_cfg = stealth_cfg or {}

        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            mins = int(remaining / 60) + 1
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error=f"Akamai cooldown — {mins}m remaining",
            )

        sku = self._extract_sku(url)
        if not sku:
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error="Could not extract SKU — use bestbuy.com/site/.../*.p URL format",
            )
        # Marketplace products (alphanumeric IDs) aren't in Best Buy's pricing API
        if not sku.isdigit():
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error="Marketplace product — only bestbuy.com/site/ products supported",
            )

        proxy = self._next_proxy(stealth_cfg)
        pid   = ProductResult.short_proxy_id(proxy)

        # Primary: priceBlocks API (no auth needed)
        price, available, error = await self._check_via_priceblocks(sku, proxy)

        if error and "CAPTCHA" in error:
            return ProductResult(
                name=name, url=url, price=None, available=False,
                blocked=True, error=error,
                proxy_id=pid, proxy_ok=False,
            )

        # Try to get image from og:image on the page (fast, usually works)
        image_url = await self._fetch_image(url, proxy)

        if price is not None:
            log.info(f"{name}: Best Buy ${price:.2f} | {'in stock' if available else 'out of stock'}")
        elif error:
            log.warning(f"{name}: {error}")
        else:
            log.warning(f"{name}: could not extract Best Buy pricing")

        return ProductResult(
            name=name,
            url=url,
            price=price,
            available=available,
            image_url=image_url,
            error=error if price is None else None,
            proxy_id=pid,
            proxy_ok=price is not None,
        )

    # ------------------------------------------------------------------
    # priceBlocks API
    # ------------------------------------------------------------------

    async def _check_via_priceblocks(self, sku: str, proxy: Optional[str]):
        """
        Call Best Buy's internal priceBlocks API.
        Returns (price, available, error).

        Note: Residential proxies fail HTTPS tunneling to Best Buy.
        We go direct — the API doesn't require proxy to bypass WAF.
        """
        api_url = f"https://www.bestbuy.com/api/3.0/priceBlocks?skus={sku}"

        try:
            from curl_cffi.requests import AsyncSession

            # Skip proxy — Best Buy API works direct, proxies fail CONNECT tunnel
            kwargs = {"headers": _API_HEADERS, "timeout": 15, "allow_redirects": True}

            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.get(api_url, **kwargs)
                ct   = resp.headers.get("content-type", "")

                # Check for Akamai block (HTML response instead of JSON)
                if "text/html" in ct:
                    body = resp.content.decode("utf-8", errors="replace")[:1000].lower()
                    if any(s in body for s in ("press & hold", "robot", "human challenge")):
                        self._cooldown_until = time.time() + _CAPTCHA_COOLDOWN
                        return None, False, "Akamai CAPTCHA — cooling down 15 min"

                if resp.status_code != 200:
                    return None, False, f"priceBlocks API returned {resp.status_code}"

                data = json.loads(resp.content.decode("utf-8", errors="replace"))
                return self._parse_priceblocks(data, sku)

        except ImportError:
            log.error("curl-cffi is required for Best Buy monitoring")
        except Exception as e:
            log.debug(f"priceBlocks API error: {e}")

        return None, False, "priceBlocks API failed"

    def _parse_priceblocks(self, data, sku: str):
        """
        Parse the priceBlocks API response.
        Returns (price, available, error).

        Successful response:
        [{"sku": {"skuId": "123", "price": {...}, "buttonState": {...}}}]

        Inactive product:
        [{"sku": {"error": "...", "skuId": "123"}}]
        """
        if not isinstance(data, list) or not data:
            return None, False, "Empty priceBlocks response"

        sku_data = data[0].get("sku", {})

        # Check for error (inactive/discontinued product)
        if "error" in sku_data:
            err = sku_data["error"]
            if "INACTIVE" in err.upper() or "inactive" in err:
                return None, False, "Product inactive/discontinued on Best Buy"
            return None, False, f"Best Buy API error: {err[:80]}"

        # Extract price
        price_obj = sku_data.get("price", {})
        price = None
        for key in ("currentPrice", "customerPrice", "salePrice",
                     "regularPrice", "price"):
            val = price_obj.get(key)
            if val is not None:
                price = self._try_float(val)
                if price:
                    break

        # Deep search fallback
        if price is None:
            price = self._deep_find_price(sku_data)

        # Extract availability
        available = False
        button = sku_data.get("buttonState", {})
        btn_state = button.get("buttonState", "").upper()
        if btn_state in ("ADD_TO_CART", "PRE_ORDER"):
            available = True
        elif btn_state in ("SOLD_OUT", "COMING_SOON", "CHECK_STORES"):
            available = False
        else:
            # Fallback: check purchasable flag
            available = bool(button.get("purchasable", False))

        return price, available, None

    # ------------------------------------------------------------------
    # Image fetch (lightweight — just og:image from the page)
    # ------------------------------------------------------------------

    async def _fetch_image(self, url: str, proxy: Optional[str]) -> Optional[str]:
        """Fetch just enough of the page to grab og:image. Goes direct (proxy tunnels fail)."""
        try:
            from curl_cffi.requests import AsyncSession
            fetch_url = self._add_nosplash(url)
            kwargs = {"headers": _API_HEADERS, "timeout": 10, "allow_redirects": True}

            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.get(fetch_url, **kwargs)
                if resp.status_code == 200:
                    html = resp.content.decode("utf-8", errors="replace")[:20_000]
                    for pat in [
                        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    ]:
                        m = re.search(pat, html, re.IGNORECASE)
                        if m and m.group(1).startswith("http"):
                            return m.group(1)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # SKU extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_sku(url: str) -> Optional[str]:
        """
        Extract SKU from Best Buy URL.
        Formats:
          https://www.bestbuy.com/site/product-name/6594498.p
          https://www.bestbuy.com/site/product-name/6594498.p?skuId=6594498
          https://www.bestbuy.com/product/product-name/JJ8VP7KGW2
          skuId= query param
        """
        # Classic format: /DIGITS.p
        m = re.search(r'/(\d{5,10})\.p', url)
        if m:
            return m.group(1)
        # Query param
        m = re.search(r'skuId=(\d+)', url)
        if m:
            return m.group(1)
        # New format: /product/product-name/ALPHANUMERIC_ID (last path segment)
        m = re.search(r'/product/[^/]+/([A-Za-z0-9]{6,})', url)
        if m:
            return m.group(1)
        # Fallback: last path segment if alphanumeric
        m = re.search(r'/([A-Za-z0-9]{6,})(?:[?&#]|$)', url)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _add_nosplash(url: str) -> str:
        sep = "&" if "?" in url else "?"
        if "intl=nosplash" not in url:
            return url + sep + "intl=nosplash"
        return url

    # ------------------------------------------------------------------
    # Proxy rotation (shared pool)
    # ------------------------------------------------------------------

    def _load_proxies(self, stealth_cfg: dict) -> None:
        if self._proxies_loaded:
            return
        self._proxies_loaded = True
        filename = stealth_cfg.get("proxies_file", "")
        if not filename:
            return
        filepath = _PROJECT_ROOT / filename
        if not filepath.exists():
            return
        for raw in filepath.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                self._proxies.append(line)
            else:
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pw = parts
                    self._proxies.append(f"http://{user}:{pw}@{host}:{port}")
        if self._proxies:
            log.info(f"Loaded {len(self._proxies)} proxies for Best Buy")

    def _next_proxy(self, stealth_cfg: dict) -> Optional[str]:
        self._load_proxies(stealth_cfg)
        if not self._proxies:
            return None
        proxy_url = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return proxy_url

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_float(val) -> Optional[float]:
        if val is None:
            return None
        try:
            if isinstance(val, str):
                val = val.replace("$", "").replace(",", "").strip()
            f = float(val)
            return f if 0.50 < f < 50_000 else None
        except (ValueError, TypeError):
            return None

    _PRICE_KEYS = {"currentPrice", "customerPrice", "regularPrice", "salePrice",
                   "price", "displayPrice"}

    def _deep_find_price(self, obj, _depth: int = 0) -> Optional[float]:
        if _depth > 12:
            return None
        if isinstance(obj, dict):
            for k in self._PRICE_KEYS:
                v = obj.get(k)
                if v is not None:
                    val = self._try_float(v)
                    if val:
                        return val
            for v in obj.values():
                r = self._deep_find_price(v, _depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = self._deep_find_price(item, _depth + 1)
                if r is not None:
                    return r
        return None
