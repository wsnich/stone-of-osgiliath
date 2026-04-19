"""
Target product price & availability monitor.

Target fetches pricing and inventory via its internal RedSky API, not
from the HTML DOM.  This monitor calls those endpoints directly using
curl-cffi with Chrome TLS impersonation + residential proxy rotation.

Flow:
  1. Extract the TCIN (Target item number) from the product URL.
  2. Call redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1
     with the static API key extracted from Target's JS bundles.
  3. Parse the JSON response for current_retail price and availability.
  4. Detect PerimeterX challenges and enter cooldown if blocked.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("target")

from monitors.walmart_monitor import ProductResult

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Static API key embedded in Target's frontend JS bundles.
# This rarely changes; if it does, extract a fresh one from any
# Target product page's network requests (look for ?key= in RedSky calls).
_REDSKY_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

_REDSKY_BASE = "https://redsky.target.com/redsky_aggregations/v1/web"

_HEADERS = {
    "User-Agent":         _UA,
    "Accept":             "application/json",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "Origin":             "https://www.target.com",
    "Referer":            "https://www.target.com/",
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-site",
    "Sec-Ch-Ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":   "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# Cooldown after PerimeterX challenge (seconds)
_CAPTCHA_COOLDOWN = 900  # 15 minutes

_PROJECT_ROOT = Path(__file__).parent.parent


class TargetMonitor:

    def __init__(self):
        self._proxy_idx = 0
        self._cooldown_until: float = 0.0
        self._proxies: list[str] = []
        self._proxies_loaded = False
        self._total_bytes: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def check_product(self, product: dict, stealth_cfg: dict | None = None) -> ProductResult:
        url  = product["url"]
        name = product["name"]
        stealth_cfg = stealth_cfg or {}

        # Respect CAPTCHA cooldown
        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            mins = int(remaining / 60) + 1
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error=f"PerimeterX cooldown — {mins}m remaining",
            )

        tcin = self._extract_tcin(url)
        if not tcin:
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error="Could not extract TCIN from Target URL",
            )

        # Priority: ISP proxy > residential > direct
        isp_list = stealth_cfg.get("isp_proxies") or []
        if isp_list:
            proxy = isp_list[self._proxy_idx % len(isp_list)]
            self._proxy_idx += 1
            if not proxy.startswith("http"):
                parts = proxy.split(":")
                if len(parts) == 4:
                    proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else:
            proxy = self._next_proxy(stealth_cfg)
        pid = ProductResult.short_proxy_id(proxy)
        store_id = stealth_cfg.get("target_store_id", "1375")  # physical store

        # Primary: pdp_client_v1 — has price + product details + images
        data = await self._call_redsky(
            f"{_REDSKY_BASE}/pdp_client_v1",
            tcin=tcin,
            proxy=proxy,
            extra_params={
                "store_id":              store_id,
                "pricing_store_id":      store_id,
                "has_pricing_store_id":  "true",
            },
        )

        # If proxy failed, retry both endpoints without proxy
        if data is None and proxy:
            log.debug(f"{name}: proxy failed, retrying Target API direct")
            data = await self._call_redsky(
                f"{_REDSKY_BASE}/pdp_client_v1",
                tcin=tcin, proxy=None,
                extra_params={
                    "store_id": store_id,
                    "pricing_store_id": store_id,
                    "has_pricing_store_id": "true",
                },
            )

        if data is None:
            data = await self._call_redsky(
                f"{_REDSKY_BASE}/product_summary_with_fulfillment_v1",
                tcin=tcin, proxy=None,
                extra_params={
                    "store_id": store_id,
                    "zip": stealth_cfg.get("zip_code", "10001"),
                },
                multi_tcin=True,
            )

        if data is None:
            log.warning(f"{name}: RedSky API returned no data")
            return ProductResult(
                name=name, url=url, price=None, available=False,
                error="RedSky API failed",
                proxy_id=pid, proxy_ok=False,
            )

        price, available, image_url = self._parse_response(data, tcin)

        if price is not None:
            log.info(f"{name}: Target ${price:.2f} | {'in stock' if available else 'out of stock'}")
        else:
            log.warning(f"{name}: could not extract Target pricing from RedSky response")

        return ProductResult(
            name=name,
            url=url,
            price=price,
            available=available,
            image_url=image_url,
            error=None if price is not None else "Could not parse Target pricing",
            proxy_id=pid,
            proxy_ok=price is not None or available,
            proxy_type="ISP" if isp_list else ("Resi" if proxy else None),
            response_bytes=self._total_bytes,
        )
        # Reset for next check
        self._total_bytes = 0

    # ------------------------------------------------------------------
    # RedSky API call
    # ------------------------------------------------------------------

    async def _call_redsky(self, endpoint: str, tcin: str, proxy: Optional[str],
                           extra_params: dict = None, multi_tcin: bool = False) -> Optional[dict]:
        params = {"key": _REDSKY_API_KEY}
        if multi_tcin:
            params["tcins"] = tcin
        else:
            params["tcin"] = tcin
        if extra_params:
            params.update(extra_params)

        query = "&".join(f"{k}={v}" for k, v in params.items())
        url   = f"{endpoint}?{query}"

        try:
            from curl_cffi.requests import AsyncSession

            kwargs = {
                "headers": _HEADERS,
                "timeout": 15,
                "allow_redirects": True,
            }
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}

            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.get(url, **kwargs)

                # CAPTCHA / bot detection
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 403:
                    body = resp.content.decode("utf-8", errors="replace")[:2000].lower()
                    if "captcha" in body or "perimeterx" in body or "press & hold" in body:
                        self._cooldown_until = time.time() + _CAPTCHA_COOLDOWN
                        log.warning("Target: CAPTCHA detected — IP flagged, cooling down 15 min")
                        return None

                self._total_bytes += len(resp.content) if resp.content else 0
                if resp.status_code == 200 and "json" in ct:
                    return json.loads(resp.content.decode("utf-8", errors="replace"))

                log.debug(f"RedSky returned {resp.status_code} ({ct})")

        except ImportError:
            log.error("curl-cffi is required for Target monitoring")
        except Exception as e:
            log.debug(f"Target RedSky API error: {e}")

        return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict, tcin: str):
        """
        Extract price, availability, and image from a RedSky API response.
        Handles both pdp_fulfillment_v1 and product_summary_with_fulfillment_v1 formats.
        Returns (price, available, image_url).
        """
        price:     Optional[float] = None
        available: bool            = False
        image_url: Optional[str]   = None

        # Navigate into the response — structure varies by endpoint
        product = self._find_product(data, tcin)
        if not product:
            return None, False, None

        # Price extraction — Target uses different fields for single vs variant products
        price_obj = product.get("price", {})
        price = self._try_float(price_obj.get("current_retail"))
        if price is None:
            price = self._try_float(price_obj.get("current_retail_min"))
        if price is None:
            price = self._try_float(price_obj.get("reg_retail"))
        if price is None:
            price = self._try_float(price_obj.get("reg_retail_min"))
        if price is None:
            # formatted_current_price may be "$17.99" or "$17.99 - $21.99"
            fcp = price_obj.get("formatted_current_price", "")
            if fcp:
                price = self._try_float(fcp.split("-")[0].strip())
        if price is None:
            price = self._deep_find_price(product)

        # Availability extraction — check multiple locations in the response
        fulfillment = product.get("fulfillment", {})

        # Check shipping availability
        shipping = fulfillment.get("shipping_options", {})
        ship_status = shipping.get("availability_status", "").upper()
        if ship_status == "IN_STOCK":
            available = True

        # Check store pickup availability
        for store in fulfillment.get("store_options", []):
            for method in ("order_pickup", "in_store_only", "ship_to_store"):
                opt = store.get(method, {})
                if opt.get("availability_status", "").upper() == "IN_STOCK":
                    available = True
                    break

        # Top-level availability fields
        avail_status = (
            product.get("availability_status", "")
            or product.get("availability", {}).get("availability_status", "")
        ).upper()
        if avail_status == "IN_STOCK":
            available = True
        elif avail_status in ("OUT_OF_STOCK", "DISCONTINUED") and not available:
            available = False

        # If no fulfillment data (pdp_client_v1 doesn't include it),
        # default to NOT available — don't assume in-stock from price alone.
        # Only explicit in-stock signals above should set available = True.

        # Image extraction
        item = product.get("item", {})
        enrichment = item.get("enrichment", {})
        images = enrichment.get("images", {})
        primary = images.get("primary_image_url")
        if primary:
            image_url = primary
        else:
            # Try alternate paths
            image_url = self._deep_find_string(product, {"primary_image_url", "imageUrl", "image_url"})

        return price, available, image_url

    def _find_product(self, data: dict, tcin: str) -> Optional[dict]:
        """Navigate the RedSky response to find the product data object."""
        # pdp_fulfillment_v1 format
        product = data.get("data", {}).get("product", {})
        if product:
            return product

        # product_summary_with_fulfillment_v1 format (array of products)
        products = data.get("data", {}).get("product_summaries", [])
        for p in products:
            if str(p.get("tcin", "")) == str(tcin):
                return p
        if products:
            return products[0]

        # Fallback: just return the data object itself
        if "price" in data or "fulfillment" in data:
            return data

        return None

    # ------------------------------------------------------------------
    # TCIN extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tcin(url: str) -> Optional[str]:
        """
        Extract Target item number from URL.
        Formats:
          https://www.target.com/p/product-name/-/A-12345678
          https://www.target.com/p/-/A-12345678
        """
        m = re.search(r'/A-(\d+)', url)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Proxy rotation (shared pool with Amazon/Walmart)
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
            log.info(f"Loaded {len(self._proxies)} proxies for Target")

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
            return f if 0.5 < f < 50_000 else None
        except (ValueError, TypeError):
            return None

    def _deep_find_price(self, obj, _depth: int = 0) -> Optional[float]:
        if _depth > 12:
            return None
        if isinstance(obj, dict):
            for k in ("current_retail", "reg_retail", "min_price",
                       "current_retail_min", "price", "offerPrice"):
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

    @staticmethod
    def _deep_find_string(obj, keys: set, _depth: int = 0) -> Optional[str]:
        if _depth > 12:
            return None
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in obj.values():
                r = TargetMonitor._deep_find_string(v, keys, _depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = TargetMonitor._deep_find_string(item, keys, _depth + 1)
                if r:
                    return r
        return None
