"""
TCGPlayer reference price monitor.

TCGPlayer now fully client-side renders — there is no __NEXT_DATA__ or
server-rendered price data in the HTML shell.  The real pricing comes from
XHR calls the browser makes after the page JS boots.

Strategy:
  1. Launch a headless Chromium (patchright) and navigate to the product URL.
  2. Intercept every JSON response from *.tcgplayer.com.
  3. Deep-search those responses for market price, lowest listing, and quantity.
  4. Return whatever we found within a 20-second window.

TCGPlayer has no meaningful bot detection (no Akamai / PerimeterX) so a plain
headless browser works fine without any stealth tricks.
"""

import asyncio
import json
import logging
import re
from typing import Optional

log = logging.getLogger("tcgplayer")

from monitors.walmart_monitor import ProductResult

# Serialize TCGPlayer checks — running multiple browser sessions through one
# residential proxy IP causes timeouts and triggers TCGPlayer's rate limits.
_tcg_check_lock = asyncio.Lock()

# Keys to look for when deep-searching JSON responses
_MARKET_KEYS = {"marketPrice", "market_price", "MarketPrice", "marketprice",
                "midPrice", "mid_price"}
_LOW_KEYS    = {"lowestListingPrice", "lowPrice", "low_price", "directLowPrice",
                "lowestPrice", "lowListingPrice", "lowestSalePrice",
                "lowestDirectPrice", "minPrice", "min_price"}
_QTY_KEYS   = {"totalListings", "listedCount", "totalQuantity", "listedQuantity",
                "quantityAvailable", "totalResults"}

# Phrases that indicate an empty box / no product listing (case-insensitive)
_EMPTY_BOX_PHRASES = [
    "box only", "no packs", "no cards", "empty box", "display only",
    "no booster", "shell only", "case only", "packaging only",
    "box and dividers only", "no product",
]


def _is_empty_box_listing(item: dict) -> bool:
    """Check if a listing is for an empty box based on customData text."""
    cd = item.get("customData") or {}
    text = ((cd.get("title") or "") + " " + (cd.get("description") or "")).lower()
    if not text.strip():
        return False
    return any(phrase in text for phrase in _EMPTY_BOX_PHRASES)


class TCGPlayerMonitor:

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def check_product(self, product: dict, stealth_cfg: dict | None = None) -> ProductResult:
        url  = product["url"]
        name = product["name"]
        tags = product.get("tags", {})

        # Append condition/printing filters from tags to the URL
        url = self._apply_filters(url, tags)

        # Serialize all TCGPlayer browser checks — concurrent sessions through
        # a single residential proxy timeout under load.
        async with _tcg_check_lock:
            # Re-check proxy cooldown INSIDE the lock — a previous serialized check
            # may have just marked the proxy bad, and we shouldn't fall through to
            # a direct hit on the home IP.
            if stealth_cfg:
                from monitors.defaults import _parse_proxy_list, get_proxy
                if _parse_proxy_list(stealth_cfg.get("proxy")) and get_proxy(stealth_cfg) is None:
                    log.info(f"{name}: proxy in cooldown — skipping TCGPlayer check")
                    return ProductResult(
                        name=name, url=url, price=None,
                        available=False,
                        error="Proxy in cooldown — check skipped",
                    )

            market_price, low_price, quantity, image_url, listing_prices, tcg_sales, price_history = await self._fetch_via_browser(url, name, stealth_cfg)

        if market_price is None and low_price is None:
            quantity = quantity or await self._fetch_quantity_api(url, stealth_cfg)
            log.warning(f"{name}: could not extract TCGPlayer pricing")
            return ProductResult(
                name=name, url=url, price=None,
                available=(quantity or 0) > 0,
                tcg_quantity=quantity,
                listing_prices=listing_prices or None,
                error="Could not parse TCGPlayer pricing",
            )

        if listing_prices:
            total_qty = sum(l.get("qty", 1) for l in listing_prices)
            log.info(f"{name}: {len(listing_prices)} listings, {total_qty} total qty")
            # Override low_price with the lowest total (price+shipping) from filtered listings
            filtered_low = min(l.get("total", l["price"]) for l in listing_prices)
            if low_price is None or filtered_low > low_price:
                log.debug(f"{name}: overriding low ${low_price} → ${filtered_low} (incl. shipping)")
                low_price = filtered_low
            quantity = len(listing_prices)

        log.info(
            f"{name}: TCGPlayer market=${market_price} "
            f"low=${low_price} qty={quantity}"
        )

        return ProductResult(
            name=name,
            url=url,
            price=market_price,
            available=(quantity or 0) > 0,
            low_price=low_price,
            listing_prices=listing_prices or None,
            tcg_sales=tcg_sales or None,
            tcg_price_history=price_history or None,
            tcg_quantity=quantity,
            image_url=image_url,
        )

    # ------------------------------------------------------------------
    # URL filter injection
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(url: str, tags: dict) -> str:
        """
        Append Condition and Printing query params from product tags
        so TCGPlayer returns filtered pricing data.
        e.g. &Condition=Near+Mint&Printing=Foil
        """
        from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        condition = tags.get("condition", "")
        printing  = tags.get("printing", "")

        if condition and "Condition" not in params:
            params["Condition"] = [condition]
        if printing and "Printing" not in params:
            params["Printing"] = [printing]

        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    # ------------------------------------------------------------------
    # Playwright browser fetch with network interception
    # ------------------------------------------------------------------

    async def _fetch_via_browser(self, url: str, name: str, stealth_cfg: dict | None = None):
        """
        Navigate to the TCGPlayer product page in a headless browser.
        Intercept the product-specific API response (contains the product ID
        in the URL) and extract pricing data from it.

        Returns (market_price, low_price, quantity, image_url).
        """
        market_price: Optional[float] = None
        low_price:    Optional[float] = None
        quantity:     Optional[int]   = None
        image_url:    Optional[str]   = None
        listing_prices: list[dict] = []      # [{price, qty}] for histogram
        tcg_sales: list[dict] = []           # [{price, date, condition}] from latestsales
        price_history_buckets: list[dict] = []  # [{date, marketPrice, low, high, sold, transactions}]
        api_listing_items: list[dict] = []   # accumulated from all listings API responses
        _seen_listing_ids: set = set()       # deduplicate across API responses

        # Extract product ID from URL to filter API responses
        product_id = None
        m = re.search(r'/product/(\d+)', url)
        if m:
            product_id = m.group(1)

        from monitors.defaults import get_user_agent, get_page_timeout, get_browser_channel

        try:
            from patchright.async_api import async_playwright as _playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _playwright
            except ImportError:
                log.warning("TCGPlayer: neither patchright nor playwright installed")
                return None, None, None, None, [], [], []

        try:
            async with _playwright() as pw:
                launch_kw = {"headless": True}
                channel = get_browser_channel(stealth_cfg)
                if channel:
                    launch_kw["channel"] = channel
                from monitors.defaults import playwright_proxy, mark_proxy_bad, mark_proxy_good, get_proxy, _parse_proxy_list
                proxy = playwright_proxy(stealth_cfg)
                _proxy_url = None
                _has_proxy_configured = bool(_parse_proxy_list(stealth_cfg.get("proxy") if stealth_cfg else None))
                if proxy:
                    raw_url = get_proxy(stealth_cfg) or ""
                    if raw_url.startswith("socks5://") and proxy.get("username"):
                        # Chromium can't do SOCKS5 with auth — use local forwarder
                        from monitors.proxy_forwarder import start_local_proxy
                        local_port = await start_local_proxy(raw_url)
                        launch_kw["proxy"] = {"server": f"socks5://127.0.0.1:{local_port}"}
                        _proxy_url = raw_url
                    else:
                        launch_kw["proxy"] = proxy
                        _proxy_url = proxy["server"]
                elif _has_proxy_configured:
                    # Proxy configured but all in cooldown — should not reach here
                    # (check_product already returns early); log and proceed without proxy
                    log.warning(f"TCGPlayer: unexpected direct hit for {name} (proxy in cooldown)")
                browser = await pw.chromium.launch(**launch_kw)
                context = await browser.new_context(
                    user_agent=get_user_agent(stealth_cfg),
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

                async def on_response(response):
                    nonlocal market_price, low_price, quantity, image_url, api_listing_items, tcg_sales, price_history_buckets
                    try:
                        resp_url = response.url
                        if "tcgplayer.com" not in resp_url:
                            return
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return

                        # Only process product-specific endpoints
                        if product_id and f"/{product_id}/" not in resp_url and f"/{product_id}?" not in resp_url:
                            return

                        body = await response.json()

                        mp = self._deep_find_price(body, _MARKET_KEYS)
                        if mp and market_price is None:
                            market_price = mp
                            log.debug(f"TCGPlayer: captured marketPrice={mp} from {resp_url}")

                        lp = self._deep_find_price(body, _LOW_KEYS)
                        if lp and low_price is None:
                            low_price = lp
                            log.debug(f"TCGPlayer: captured lowPrice={lp} from {resp_url}")

                        q = self._deep_find_int(body, _QTY_KEYS)
                        if q and quantity is None:
                            quantity = q

                        # Accumulate individual listing items across ALL API responses
                        if "listing" in resp_url.lower():
                            outer = body.get("results", [])
                            if outer and isinstance(outer, list):
                                top = outer[0] if isinstance(outer[0], dict) else {}
                                items = top.get("results", [])
                                if items and isinstance(items, list):
                                    new_count = 0
                                    for item in items:
                                        lid = item.get("listingId")
                                        if lid and lid not in _seen_listing_ids:
                                            _seen_listing_ids.add(lid)
                                            if _is_empty_box_listing(item):
                                                log.debug(f"TCGPlayer: filtered empty-box listing #{lid}")
                                                continue
                                            api_listing_items.append(item)
                                            new_count += 1
                                    if new_count:
                                        log.debug(f"TCGPlayer: +{new_count} listing items (total {len(api_listing_items)})")

                        # Capture recent sales from latestsales endpoint
                        if "latestsales" in resp_url.lower() and not tcg_sales:
                            sales_list = body.get("data", [])
                            if isinstance(sales_list, list):
                                for s in sales_list:
                                    try:
                                        tcg_sales.append({
                                            "price": float(s.get("purchasePrice", 0)),
                                            "date": s.get("orderDate", ""),
                                            "condition": s.get("condition", ""),
                                            "variant": s.get("variant", ""),
                                        })
                                    except (ValueError, TypeError):
                                        pass
                                if tcg_sales:
                                    log.debug(f"TCGPlayer: captured {len(tcg_sales)} recent sales")

                        # Capture price history buckets (daily aggregated sale data)
                        if "price/history" in resp_url.lower() and not price_history_buckets:
                            results = body.get("result", [])
                            if results and isinstance(results, list):
                                for bucket in results[0].get("buckets", []):
                                    try:
                                        sold = int(bucket.get("quantitySold", 0))
                                        if sold > 0:
                                            price_history_buckets.append({
                                                "date": bucket.get("bucketStartDate", ""),
                                                "marketPrice": float(bucket.get("marketPrice", 0)),
                                                "low": float(bucket.get("lowSalePrice", 0)),
                                                "high": float(bucket.get("highSalePrice", 0)),
                                                "sold": sold,
                                                "transactions": int(bucket.get("transactionCount", 0)),
                                            })
                                    except (ValueError, TypeError):
                                        pass
                                if price_history_buckets:
                                    log.debug(f"TCGPlayer: captured {len(price_history_buckets)} price history buckets")

                    except Exception:
                        pass  # non-JSON or response body already consumed

                page.on("response", on_response)

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=get_page_timeout(stealth_cfg))
                    # Extra wait for JS/API calls to fire after DOM is ready
                    await asyncio.sleep(5)
                except Exception as _nav_err:
                    if _proxy_url:
                        mark_proxy_bad(_proxy_url)
                        log.warning(f"TCGPlayer: proxy {_proxy_url} failed ({_nav_err})")
                    raise

                # Brief extra wait so late-firing XHR calls can complete
                await asyncio.sleep(3)

                # Navigation succeeded — reset proxy failure counter
                if _proxy_url:
                    mark_proxy_good(_proxy_url)

                # Scrape filtered prices from the DOM — these reflect
                # Condition/Printing filters that the API ignores
                dom_market, dom_low, dom_qty = await self._scrape_filtered_prices(page)
                if dom_market is not None:
                    market_price = dom_market
                    log.debug(f"TCGPlayer: DOM filtered market=${dom_market}")
                if dom_low is not None:
                    low_price = dom_low
                    log.debug(f"TCGPlayer: DOM filtered low=${dom_low}")
                if dom_qty is not None:
                    quantity = dom_qty
                    log.debug(f"TCGPlayer: DOM filtered qty={dom_qty}")

                # If API captured first page, paginate to capture the rest
                # (clicking Next triggers new API calls that on_response accumulates)
                total_expected = quantity or 0
                if api_listing_items and len(api_listing_items) < total_expected:
                    for _ in range(20):  # safety cap
                        has_next = await page.evaluate(r"""() => {
                            const pagination = document.querySelector('.tcg-pagination, [class*="pagination"]');
                            if (!pagination) return false;
                            const buttons = pagination.querySelectorAll('a, button');
                            for (const btn of buttons) {
                                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                                if (label.includes('next') && !btn.disabled && !btn.classList.contains('is-disabled')) {
                                    btn.click();
                                    return true;
                                }
                            }
                            return false;
                        }""")
                        if not has_next:
                            break
                        await asyncio.sleep(2)  # wait for API response + on_response handler
                        if len(api_listing_items) >= total_expected:
                            break

                # Convert captured API listing items to [{price, qty}]
                if api_listing_items:
                    listing_prices = []
                    for item in api_listing_items:
                        if _is_empty_box_listing(item):
                            continue
                        p = item.get("price") or item.get("sellerPrice")
                        q = item.get("quantity", 1)
                        s = item.get("shippingPrice", 0)
                        try:
                            p = float(p)
                            q = int(q)
                            s = float(s or 0)
                        except (ValueError, TypeError):
                            continue
                        if 0 <= p < 50_000:
                            listing_prices.append({
                                "price": p,
                                "shipping": round(s, 2),
                                "total": round(p + s, 2),
                                "qty": max(1, q),
                            })
                    listing_prices.sort(key=lambda x: x["total"])
                    total_api = sum(l["qty"] for l in listing_prices)
                    log.info(f"{name}: API → {len(listing_prices)} listings, {total_api} total qty")
                else:
                    # Fallback: paginate DOM (qty will be 1 for each)
                    listing_prices = await self._scrape_listing_prices(page)

                # Try to grab image from og:image if we don't have one
                if image_url is None:
                    try:
                        img = await page.get_attribute(
                            'meta[property="og:image"]', "content", timeout=2000
                        )
                        if img and img.startswith("http"):
                            image_url = img
                    except Exception:
                        pass

                # Fallback DOM scraping if nothing found yet
                if market_price is None and low_price is None:
                    market_price, low_price, quantity = await self._scrape_dom(page)

                await context.close()
                await browser.close()

        except Exception as e:
            log.warning(f"TCGPlayer browser fetch error: {e}")

        return market_price, low_price, quantity, image_url, listing_prices, tcg_sales, price_history_buckets

    # ------------------------------------------------------------------
    # Listing price scraping (individual seller prices for histogram)
    # ------------------------------------------------------------------

    async def _scrape_listing_prices(self, page) -> list[dict]:
        """
        Scrape ALL listing prices + quantities from TCGPlayer.
        Paginates through all pages, reading price + select-option-count
        from each listing row.  Returns [{price, qty}] sorted by price.
        """
        try:
            # Try to switch to 50/page via the custom Vue dropdown
            try:
                dropdown = page.locator('[class*="listings-per-page"] .tcg-input-select').first
                await dropdown.click(timeout=3000)
                await asyncio.sleep(0.5)
                # Click the "50" option from the dropdown overlay
                opt50 = page.locator('.tcg-input-select__option').filter(has_text="50").first
                await opt50.click(timeout=3000)
                log.debug("TCGPlayer: switched to 50 listings/page")
                await asyncio.sleep(3)
            except Exception:
                pass  # continue with default 10/page

            all_listings = []
            seen_prices_hash = set()  # detect duplicate pages
            max_pages = 20

            for page_num in range(max_pages):
                page_listings = await page.evaluate(r"""() => {
                    const results = [];
                    document.querySelectorAll('.listing-item').forEach(row => {
                        let price = null, qty = 1;
                        const priceEl = row.querySelector('.listing-item__listing-data__info__price');
                        if (priceEl) {
                            const m = priceEl.textContent.match(/\$([\d,]+\.?\d*)/);
                            if (m) price = parseFloat(m[1].replace(',',''));
                        }
                        // Quantity = number of options in the qty select dropdown
                        const sel = row.querySelector('select');
                        if (sel && sel.options.length > 0) {
                            qty = sel.options.length;
                        }
                        if (price != null && price >= 0 && price < 50000) {
                            results.push({price, qty: Math.max(1, qty)});
                        }
                    });
                    return results;
                }""")

                if not page_listings:
                    break

                # Detect duplicate page (pagination didn't advance)
                page_hash = str([(l["price"], l["qty"]) for l in page_listings])
                if page_hash in seen_prices_hash:
                    break
                seen_prices_hash.add(page_hash)

                all_listings.extend(page_listings)
                log.debug(f"TCGPlayer: page {page_num + 1} → {len(page_listings)} listings")

                # Click "Next page" if available
                has_next = await page.evaluate(r"""() => {
                    const pagination = document.querySelector('.tcg-pagination, [class*="pagination"]');
                    if (!pagination) return false;
                    const buttons = pagination.querySelectorAll('a, button');
                    for (const btn of buttons) {
                        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (label.includes('next') && !btn.disabled && !btn.classList.contains('is-disabled')) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }""")

                if not has_next:
                    break
                await asyncio.sleep(2)

            all_listings.sort(key=lambda x: x["price"])
            return all_listings
        except Exception as e:
            log.debug(f"TCGPlayer listing scrape error: {e}")
            return []

    # ------------------------------------------------------------------
    # DOM price scraping (reads filter-aware prices from rendered page)
    # ------------------------------------------------------------------

    async def _scrape_filtered_prices(self, page):
        """
        Read market price, lowest listing, and filtered quantity from
        TCGPlayer's rendered DOM.  These reflect Condition/Printing URL
        filters that the API ignores.

        Key elements:
          .charts-price          — market price ("Near Mint Foil $18.34")
          listing-count element  — "36 Listings As low as $18.34"
        """
        try:
            result = await page.evaluate(r"""() => {
                let market = null, low = null, qty = null;

                // Market price from chart label
                const chartEl = document.querySelector('.charts-price');
                if (chartEl) {
                    const m = chartEl.textContent.match(/\$([\d,]+\.?\d*)/);
                    if (m) market = parseFloat(m[1].replace(',',''));
                }

                // Listing count + lowest price from the listing count element
                // Matches: "36 Listings As low as $18.34"
                document.querySelectorAll('*').forEach(el => {
                    const t = el.textContent.trim();
                    if (t.match(/^\d+\s+Listing/i) && el.children.length <= 2 && t.length < 80) {
                        const qm = t.match(/^(\d+)\s+Listing/i);
                        if (qm && qty === null) qty = parseInt(qm[1]);
                        const lm = t.match(/low\s+as\s+\$([\d,]+\.?\d*)/i);
                        if (lm && low === null) low = parseFloat(lm[1].replace(',',''));
                    }
                });

                return { market, low, qty };
            }""")

            market = result.get("market")
            low = result.get("low")
            qty = result.get("qty")

            if market and not (0.5 < market < 50_000):
                market = None
            if low and not (0.5 < low < 50_000):
                low = None

            return market, low, qty
        except Exception:
            return None, None, None

    # ------------------------------------------------------------------
    # DOM scraping fallback (if network interception captures nothing)
    # ------------------------------------------------------------------

    async def _scrape_dom(self, page):
        """
        Last-resort DOM scrape.  TCGPlayer's class names change with
        each deploy, so we use broad text-content patterns.
        """
        market = low = qty = None
        try:
            html = await page.content()

            m = re.search(r'Market\s*Price[^<]*\$\s*([\d,]+\.?\d*)', html, re.IGNORECASE)
            if m:
                market = self._parse_price(m.group(1))

            m = re.search(r'Lowest\s*Listing[^<]*\$\s*([\d,]+\.?\d*)', html, re.IGNORECASE)
            if m:
                low = self._parse_price(m.group(1))

            m = re.search(r'(\d+)\s+(?:Listing|listing)', html)
            if m:
                try:
                    qty = int(m.group(1))
                except ValueError:
                    pass

        except Exception as e:
            log.debug(f"TCGPlayer DOM scrape error: {e}")

        return market, low, qty

    async def _fetch_quantity_api(self, url: str, stealth_cfg: dict | None = None) -> Optional[int]:
        """Fallback: get totalResults count via simple POST API."""
        product_id = self._extract_product_id(url)
        if not product_id:
            return None
        api_url = f"https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"
        body = {"mpfev": 3, "channel": 0, "language": 1, "start": 0, "rows": 1}
        try:
            from curl_cffi.requests import AsyncSession
            from monitors.defaults import get_proxy, mark_proxy_bad
            _proxy = get_proxy(stealth_cfg)
            async with AsyncSession(impersonate="chrome124", proxy=_proxy) as s:
                r = await s.post(api_url, json=body, timeout=10,
                                 headers={
                                     "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                                     "Accept":       "application/json",
                                     "Origin":       "https://www.tcgplayer.com",
                                     "Referer":      "https://www.tcgplayer.com/",
                                 })
                if r.status_code == 200:
                    data = json.loads(r.content.decode("utf-8", errors="replace"))
                    results = data.get("results", [{}])
                    if results:
                        return results[0].get("totalResults")
        except Exception as e:
            if _proxy:
                mark_proxy_bad(_proxy)
                log.debug(f"TCGPlayer quantity API proxy {_proxy} failed — marked bad")
            log.debug(f"TCGPlayer quantity API error: {e}")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_product_id(url: str) -> Optional[str]:
        m = re.search(r'/product/(\d+)', url)
        return m.group(1) if m else None

    def _deep_find_price(self, obj, keys: set, _depth: int = 0) -> Optional[float]:
        if _depth > 20:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and v is not None:
                    val = self._parse_price(str(v))
                    if val:
                        return val
            for v in obj.values():
                r = self._deep_find_price(v, keys, _depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = self._deep_find_price(item, keys, _depth + 1)
                if r is not None:
                    return r
        return None

    def _deep_find_int(self, obj, keys: set, _depth: int = 0) -> Optional[int]:
        if _depth > 20:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and v is not None:
                    try:
                        return int(v)
                    except (ValueError, TypeError):
                        pass
            for v in obj.values():
                r = self._deep_find_int(v, keys, _depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = self._deep_find_int(item, keys, _depth + 1)
                if r is not None:
                    return r
        return None

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(r'\d+\.?\d*', str(text).replace(",", ""))
        if m:
            try:
                val = float(m.group())
                if 0.5 < val < 50_000:
                    return val
            except ValueError:
                pass
        return None
