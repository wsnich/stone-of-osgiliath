"""
Amazon product price & availability monitor.

Architectural constraints:
  1. curl-cffi with impersonate="chrome124" (AWS WAF TLS fingerprint check)
  2. Residential proxy rotation (Amazon IP reputation system)
  3. JSON extraction only (__INITIAL_STATE__, JSON-LD, inline JSON blobs)
     — no brittle DOM/CSS selectors via BeautifulSoup
  4. CAPTCHA detection with auto-pause + cooldown (no API/PA-API)

Supported URL formats:
  https://www.amazon.com/dp/B0XXXXXX
  https://www.amazon.com/Some-Product/dp/B0XXXXXX/ref=...
  https://www.amazon.com/gp/product/B0XXXXXX
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("amazon")

from monitors.walmart_monitor import ProductResult

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent":                _UA,
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "DNT":                       "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":          "?0",
    "Sec-Ch-Ua-Platform":        '"Windows"',
}

# Cooldown after CAPTCHA detection (seconds)
_CAPTCHA_COOLDOWN = 900  # 15 minutes


_PROJECT_ROOT = Path(__file__).parent.parent


class AmazonMonitor:

    def __init__(self):
        self._proxy_idx = 0
        self._cooldown_until: float = 0.0
        self._proxies: list[str] = []
        self._proxies_loaded = False
        self._last_proxy_url: Optional[str] = None
        self._last_proxy_ok:  Optional[bool] = None

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
                error=f"CAPTCHA cooldown — {mins}m remaining",
            )

        # Playwright with DOM extraction — Amazon loads prices entirely via JS,
        # so static HTML parsing is unreliable and produces false positives.
        # Browser check is the ONLY reliable path.
        self._last_proxy_url = None
        self._last_proxy_ok  = None
        price, available, image_url, high_price = await self._fetch_via_browser(url, name, stealth_cfg)

        pid = ProductResult.short_proxy_id(self._last_proxy_url)

        if price is not None:
            log.info(f"{name}: Amazon ${price:.2f} | {'in stock' if available else 'out of stock'}")
            return ProductResult(
                name=name, url=url, price=price,
                available=available, image_url=image_url,
                proxy_id=pid, proxy_ok=self._last_proxy_ok, proxy_type=None,
            )

        if high_price:
            log.info(f"{name}: Amazon — High Price (no Buy Box, product available)")
            return ProductResult(
                name=name, url=url, price=None, available=True,
                image_url=image_url,
                error="High Price",
                proxy_id=pid, proxy_ok=self._last_proxy_ok, proxy_type=None,
            )

        log.warning(f"{name}: could not extract Amazon pricing via browser")
        return ProductResult(
            name=name, url=url, price=None, available=False,
            image_url=image_url,
            error="Browser could not load Amazon price",
            proxy_id=pid, proxy_ok=self._last_proxy_ok,
        )

    # ------------------------------------------------------------------
    # Playwright browser fetch with DOM extraction
    # ------------------------------------------------------------------

    async def _fetch_via_browser(self, url: str, name: str, stealth_cfg: dict):
        """
        Amazon loads Buy Box prices dynamically via JS.  Launch a headless
        browser, wait for the price to render, then extract from the DOM.

        Returns (price, available, image_url).
        """
        price:      Optional[float] = None
        available:  bool            = False
        image_url:  Optional[str]   = None
        high_price: bool            = False

        try:
            from patchright.async_api import async_playwright as _pw
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _pw
            except ImportError:
                log.debug("Amazon: no playwright available, skipping browser check")
                return None, False, None, False

        try:
            import asyncio as _aio
            async with _pw() as pw:
                headless = stealth_cfg.get("headless", True)

                # Amazon: go direct — ISP proxies geolocate differently and
                # show wrong storefront/prices. Direct browser works fine.
                proxy_url = None
                browser = None
                page    = None

                for attempt_proxy in ([proxy_url, None] if proxy_url else [None]):
                    lk = {"headless": headless}
                    if attempt_proxy:
                        parsed = self._parse_proxy_url(attempt_proxy)
                        if parsed:
                            lk["proxy"] = parsed

                    # Launch browser
                    for channel in ("chrome", "msedge", None):
                        try:
                            kw = {**lk}
                            if channel:
                                kw["channel"] = channel
                            browser = await pw.chromium.launch(**kw)
                            break
                        except Exception:
                            continue
                    else:
                        continue

                    context = await browser.new_context(
                        user_agent=_UA,
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                    )
                    page = await context.new_page()

                    try:
                        await page.goto(url, wait_until="networkidle", timeout=40_000)
                        await _aio.sleep(3)
                        self._last_proxy_url = attempt_proxy
                        self._last_proxy_ok  = True
                        break  # success
                    except Exception as e:
                        log.debug(f"Amazon browser nav failed ({'proxy' if attempt_proxy else 'direct'}): {e}")
                        if attempt_proxy:
                            self._last_proxy_url = attempt_proxy
                            self._last_proxy_ok  = False
                        await context.close()
                        await browser.close()
                        browser = page = None
                        continue

                if not page:
                    return None, False, None, False
                # Wait for the Buy Box price to render
                try:
                    await page.wait_for_selector(
                        '#corePrice_desktop .a-price .a-offscreen, '
                        '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, '
                        '.a-price[data-a-size="xl"] .a-offscreen, '
                        '#price_inside_buybox, '
                        '#newBuyBoxPrice',
                        timeout=12_000,
                    )
                except Exception:
                    log.debug(f"{name}: Buy Box price selector didn't appear in 12s")

                # ── Step 1: Try standard Buy Box price ──
                price_text = await page.evaluate(r'''
                    () => {
                        const selectors = [
                            '#corePrice_desktop .a-price .a-offscreen',
                            '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
                            '#price_inside_buybox',
                            '#newBuyBoxPrice',
                            '.a-price[data-a-size="xl"] .a-offscreen',
                            '.a-price[data-a-size="xxl"] .a-offscreen',
                            '#buyNew_noncbb .a-price .a-offscreen',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.textContent.trim()) return el.textContent.trim();
                        }
                        return null;
                    }
                ''')

                if price_text:
                    price = self._parse_price(price_text)
                    log.debug(f"{name}: Buy Box price='{price_text}' -> ${price}")

                # ── Step 2: No Buy Box — click "See All Buying Options" ──
                if price is None:
                    # Snapshot prices BEFORE clicking
                    before_prices = set(await page.evaluate(r'''
                        () => {
                            const p = [];
                            document.querySelectorAll('.a-price .a-offscreen').forEach(el => {
                                p.push(el.textContent.trim());
                            });
                            return p;
                        }
                    '''))

                    clicked = False
                    for sel in [
                        '#buybox-see-all-buying-choices a',
                        '#buybox-see-all-buying-choices-announce',
                        'a:has-text("See All Buying Options")',
                    ]:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                clicked = True
                                log.debug(f"{name}: clicked See All Buying Options")
                                break
                        except Exception:
                            pass

                    if clicked:
                        # Wait for AOD container to appear
                        try:
                            await page.wait_for_selector(
                                '#aod-offer-list, #aod-container',
                                timeout=10_000,
                            )
                        except Exception:
                            log.debug(f"{name}: AOD container didn't appear")
                        await _aio.sleep(3)

                        # Extract condition+price from AOD offer list
                        # Amazon uses .a-price-whole + .a-price-fraction (NOT .a-offscreen)
                        aod_offers = await page.evaluate(r'''
                            () => {
                                const offerList = document.querySelector("#aod-offer-list");
                                if (!offerList) return [];

                                let condition = "New";
                                const results = [];

                                function walk(el) {
                                    if (el.id && el.id.includes("aod-offer-heading")) {
                                        const t = el.textContent.trim().split("\n")[0].trim();
                                        if (["New","Used","Collectible","Renewed"].includes(t))
                                            condition = t;
                                    }
                                    if (el.id && el.id.includes("aod-offer-price")) {
                                        const whole = el.querySelector(".a-price-whole");
                                        const frac  = el.querySelector(".a-price-fraction");
                                        if (whole) {
                                            const w = whole.textContent.replace(/[^0-9]/g, "");
                                            const f = frac ? frac.textContent.replace(/[^0-9]/g, "") : "00";
                                            results.push({ condition, price: parseFloat(w + "." + f) });
                                        }
                                    }
                                    for (const child of el.children || []) walk(child);
                                }
                                walk(offerList);
                                return results;
                            }
                        ''')

                        new_offers = [o["price"] for o in aod_offers
                                      if o.get("condition") == "New" and o["price"] > 1]
                        if new_offers:
                            new_offers.sort()
                            price = new_offers[0]
                            log.debug(f"{name}: AOD New offers={new_offers}, lowest=${price}")
                        elif aod_offers:
                            # No "New" offers — fall back to all conditions
                            all_p = sorted(o["price"] for o in aod_offers if o["price"] > 1)
                            if all_p:
                                price = all_p[0]
                                log.debug(f"{name}: AOD all offers={all_p}, lowest=${price}")

                # ── Step 3: Detect "High Price" when no price found ──
                if price is None:
                    is_product_page = await page.evaluate(r'''
                        () => !!document.querySelector('#productTitle, #title, [data-feature-name="title"]')
                    ''')
                    if is_product_page:
                        high_price = True
                        available  = True
                        log.debug(f"{name}: no Buy Box price, product page valid → High Price")

                # ── Step 4: Availability ──
                if not high_price:
                    has_cart = await page.evaluate(r'''
                        () => !!document.querySelector('#add-to-cart-button, input[name="submit.add-to-cart"]')
                    ''')
                    if has_cart:
                        available = True
                    else:
                        available = price is not None

                # CAPTCHA check
                page_text = await page.evaluate('() => document.title')
                if page_text and 'robot' in page_text.lower():
                    self._cooldown_until = time.time() + _CAPTCHA_COOLDOWN
                    log.warning(f"{name}: Amazon CAPTCHA detected in browser")
                    await context.close()
                    await browser.close()
                    return None, False, None, False

                # Image
                img = await page.evaluate(r'''
                    () => {
                        const el = document.querySelector('#landingImage, #imgBlkFront, #main-image');
                        return el ? el.src : null;
                    }
                ''')
                if img and img.startswith("http"):
                    image_url = img

                await context.close()
                await browser.close()

        except Exception as e:
            log.warning(f"Amazon browser error: {e}")

        return price, available, image_url, high_price

    # ------------------------------------------------------------------
    # HTTP fetch via curl-cffi
    # ------------------------------------------------------------------

    async def _fetch(self, url: str, proxy: Optional[str] = None) -> Optional[str]:
        try:
            from curl_cffi.requests import AsyncSession

            kwargs = {
                "headers":         _HEADERS,
                "timeout":         20,
                "allow_redirects": True,
                "impersonate":     "chrome124",
            }
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}

            async with AsyncSession() as session:
                resp = await session.get(url, **kwargs)
                if resp.status_code == 200:
                    return resp.content.decode("utf-8", errors="replace")
                log.debug(f"Amazon fetch returned {resp.status_code}")
        except ImportError:
            log.error("curl-cffi is required for Amazon monitoring — pip install curl-cffi")
        except Exception as e:
            log.warning(f"Amazon fetch error: {e}")
        return None

    # ------------------------------------------------------------------
    # Proxy rotation
    # ------------------------------------------------------------------

    def _load_proxies(self, stealth_cfg: dict) -> None:
        """
        Load proxies once from the file specified in amazon_proxies_file.
        Accepts two formats per line:
          - host:port:user:pass  → auto-converts to http://user:pass@host:port
          - http://user:pass@host:port  → used as-is
        Blank lines and # comments are skipped.
        """
        if self._proxies_loaded:
            return
        self._proxies_loaded = True

        filename = stealth_cfg.get("proxies_file", "amazon_proxies.txt")
        filepath = _PROJECT_ROOT / filename
        if not filepath.exists():
            log.warning(f"Amazon proxy file not found: {filepath}")
            return

        lines = filepath.read_text(encoding="utf-8").splitlines()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                self._proxies.append(line)
            else:
                # Convert host:port:user:pass → http://user:pass@host:port
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pw = parts
                    self._proxies.append(f"http://{user}:{pw}@{host}:{port}")
                else:
                    log.debug(f"Skipping unrecognised proxy line: {line[:60]}")

        log.info(f"Loaded {len(self._proxies)} Amazon proxies from {filepath.name}")

    def _next_proxy(self, stealth_cfg: dict) -> Optional[str]:
        self._load_proxies(stealth_cfg)
        if not self._proxies:
            return None
        proxy_url = self._proxies[self._proxy_idx % len(self._proxies)]
        self._proxy_idx += 1
        return proxy_url

    @staticmethod
    def _parse_proxy_url(url: str) -> Optional[dict]:
        """Convert http://user:pass@host:port into Playwright proxy dict."""
        m = re.match(r'https?://([^:]+):([^@]+)@([^:]+):(\d+)', url)
        if m:
            return {
                "server": f"http://{m.group(3)}:{m.group(4)}",
                "username": m.group(1),
                "password": m.group(2),
            }
        return None

    # ------------------------------------------------------------------
    # CAPTCHA / bot detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_captcha(html: str) -> bool:
        lower = html.lower()
        signals = (
            "captcha",
            "robot check",
            "to discuss automated access",
            "api-services-support@amazon.com",
            "type the characters you see",
            "solve this puzzle",
            "verify you are a human",
        )
        for s in signals:
            if s in lower:
                # Don't false-positive on normal product pages that
                # mention "captcha" in a script or footer link
                if s == "captcha":
                    # Only flag if it appears in a <form> or <title> context
                    if re.search(r'<(title|h[1-4]|form|label)[^>]*>[^<]*captcha',
                                 html, re.IGNORECASE):
                        return True
                else:
                    return True
        return False

    # ------------------------------------------------------------------
    # Pricing & availability extraction
    # ------------------------------------------------------------------

    def _extract_pricing(self, html: str):
        """
        Returns (price, available).

        Amazon pages contain many prices: Buy Box, third-party sellers,
        related products, single-card listings, etc.  We must specifically
        target the *main product Buy Box price* — not the lowest price
        on the page.

        Priority order:
          1. Buy Box text patterns (priceToPay, corePrice — most reliable)
          2. JSON-LD Product schema (skip AggregateOffer.lowPrice)
          3. Inline JSON blobs (targeted keys only)
        """
        # 1. Buy Box text patterns — directly from the rendered HTML
        price = self._from_buybox_patterns(html)
        if price is not None:
            available = self._check_availability_text(html)
            log.debug(f"Amazon: Buy Box pattern price=${price}")
            return price, available

        # 2. JSON-LD
        price, available = self._from_json_ld(html)
        if price is not None:
            return price, available

        # 3. Inline JSON blobs (targeted)
        price, available = self._from_inline_json(html)
        if price is not None:
            return price, available

        return None, False

    # -- Source 1: Buy Box text patterns (highest priority) ---

    def _from_buybox_patterns(self, html: str) -> Optional[float]:
        """
        Extract the main Buy Box price from Amazon's HTML.
        These patterns match the prominently-displayed price, not other
        sellers or related products.
        """
        patterns = [
            # priceToPay is Amazon's primary Buy Box price element
            r'priceToPay[^>]*>\s*<[^>]*>\s*<[^>]*>\$\s*([\d,]+)\s*</[^>]*>\s*<[^>]*>(\d{2})',
            r'priceToPay[^>]*>.*?\$\s*([\d,]+\.?\d{0,2})',
            # a-price-whole + a-price-fraction inside corePriceDisplay
            r'corePriceDisplay.*?a-price-whole["\s>]+(\d[\d,]*).*?a-price-fraction["\s>]+(\d{2})',
            # corePrice_feature_div is the wrapper for the main price
            r'corePrice_feature_div.*?a-price-whole["\s>]+(\d[\d,]*).*?a-price-fraction["\s>]+(\d{2})',
            # Direct corePrice display
            r'corePrice[^>]*>.*?\$\s*([\d,]+\.?\d{0,2})',
            # priceblock (older Amazon format)
            r'priceblock_ourprice[^>]*>.*?\$\s*([\d,]+\.?\d{0,2})',
            r'priceblock_dealprice[^>]*>.*?\$\s*([\d,]+\.?\d{0,2})',
        ]

        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if m:
                if m.lastindex == 2:
                    price = self._parse_price(f"{m.group(1)}.{m.group(2)}")
                else:
                    price = self._parse_price(m.group(1))
                if price:
                    return price
        return None

    # -- Source 2: JSON-LD ---

    def _from_json_ld(self, html: str):
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            try:
                data  = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Product", "IndividualProduct"):
                        continue

                    offers  = item.get("offers", {})
                    targets = offers if isinstance(offers, list) else [offers]

                    for o in targets:
                        offer_type = o.get("@type", "")

                        if offer_type == "AggregateOffer":
                            # AggregateOffer.lowPrice = cheapest third-party listing,
                            # NOT the main product price.  Only use "price" if present.
                            price = self._parse_price(str(o.get("price", "")))
                            # Skip lowPrice entirely — it's often a single card/pack
                        else:
                            # Regular Offer — price is the Buy Box price
                            price = self._parse_price(str(o.get("price", "")))

                        avail_str = str(o.get("availability", "")).lower()
                        available = "instock" in avail_str or "limitedavailability" in avail_str

                        if price is not None:
                            log.debug(f"Amazon: JSON-LD ({offer_type}) price=${price}")
                            return price, available
            except Exception:
                continue
        return None, False

    # -- Source 3: Inline JSON blobs (targeted) ---

    # Buy Box specific keys — not generic "price"
    _BUYBOX_KEYS = {"priceToPay", "ourPrice", "buyingPrice", "dealPrice",
                    "currentPrice", "priceAmount", "salePrice"}

    def _from_inline_json(self, html: str):
        for var_name in ("__INITIAL_STATE__", "__PRELOADED_STATE__",
                         "buyingOptionState"):
            m = re.search(
                rf'(?:window\.)?{re.escape(var_name)}\s*=\s*(\{{.*?\}});',
                html, re.DOTALL,
            )
            if m:
                try:
                    data  = json.loads(m.group(1))
                    price = self._deep_find_buybox_price(data)
                    if price is not None:
                        avail = self._deep_find_availability(data)
                        log.debug(f"Amazon: {var_name} price=${price}")
                        return price, avail
                except Exception:
                    continue
        return None, False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @staticmethod
    def _check_availability_text(html: str) -> bool:
        lower = html.lower()
        # Strong out-of-stock signals
        for pat in ("currently unavailable", "out of stock",
                    "unavailable for purchase"):
            if pat in lower:
                return False
        # Strong in-stock signals
        for pat in ("add to cart", "buy now", "in stock"):
            if pat in lower:
                return True
        return False

    def _deep_find_availability(self, obj, _depth: int = 0) -> bool:
        if _depth > 15:
            return False
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if kl in ("availability", "availabilitystatus", "instockstatus"):
                    vs = str(v).lower()
                    if "instock" in vs or "in_stock" in vs or vs == "true":
                        return True
                    if "outofstock" in vs or "out_of_stock" in vs or "unavailable" in vs:
                        return False
            for v in obj.values():
                r = self._deep_find_availability(v, _depth + 1)
                if r:
                    return True
        elif isinstance(obj, list):
            for item in obj:
                r = self._deep_find_availability(item, _depth + 1)
                if r:
                    return True
        return False

    # ------------------------------------------------------------------
    # Image extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_image(html: str) -> Optional[str]:
        # JSON-LD image
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            try:
                data  = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") in ("Product", "IndividualProduct"):
                        img = item.get("image")
                        if isinstance(img, list):
                            img = img[0]
                        if isinstance(img, str) and img.startswith("http"):
                            return img
            except Exception:
                continue

        # og:image meta tag
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m and m.group(1).startswith("http"):
                return m.group(1)

        # data-a-dynamic-image (Amazon's main product image JSON)
        m = re.search(r'data-a-dynamic-image=["\'](\{[^"\']+\})["\']', html)
        if m:
            try:
                imgs = json.loads(m.group(1).replace("&quot;", '"'))
                # Return the first URL (usually the largest)
                for url in imgs:
                    if url.startswith("http"):
                        return url
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------
    # Deep JSON price search (Buy Box keys only)
    # ------------------------------------------------------------------

    def _deep_find_buybox_price(self, obj, _depth: int = 0) -> Optional[float]:
        """
        Search JSON for Buy Box specific keys only.
        Avoids generic "price" / "lowPrice" which match third-party listings.
        """
        if _depth > 15:
            return None
        if isinstance(obj, dict):
            # Check Buy Box specific keys first
            for k in ("priceToPay", "ourPrice", "buyingPrice", "dealPrice",
                       "currentPrice", "priceAmount", "salePrice"):
                v = obj.get(k)
                if v is None:
                    continue
                if isinstance(v, dict):
                    inner = v.get("value") or v.get("amount") or v.get("price")
                    if inner is not None:
                        val = self._parse_price(str(inner))
                        if val:
                            return val
                else:
                    val = self._parse_price(str(v))
                    if val:
                        return val

            for v in obj.values():
                r = self._deep_find_buybox_price(v, _depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = self._deep_find_buybox_price(item, _depth + 1)
                if r is not None:
                    return r
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = str(text).replace(",", "").replace("$", "").strip()
        m = re.search(r'\d+\.?\d*', cleaned)
        if m:
            try:
                val = float(m.group())
                if 0.50 < val < 50_000:
                    return val
            except ValueError:
                pass
        return None
