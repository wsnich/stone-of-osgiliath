"""
eBay sold/completed listings monitor.

Searches eBay for recently sold items matching a product name,
extracts sale prices via Playwright (eBay client-side renders prices),
and returns aggregate stats (median, average, count).

Used for market intelligence — not for purchasing. Works for MTG
singles, sealed product, comics, or any eBay-searchable item.
"""

import logging
import re
import statistics
from typing import Optional

log = logging.getLogger("ebay")


class EbaySoldResult:
    """Aggregate of recent sold + live listings."""
    def __init__(self, sales: list[dict] = None, live: list[dict] = None):
        self.sales: list[dict] = sales or []
        self.live:  list[dict] = live or []

    @property
    def count(self) -> int:
        return len(self.sales)

    @property
    def prices(self) -> list[float]:
        return [s["price"] for s in self.sales if s.get("price")]

    @property
    def median(self) -> Optional[float]:
        p = self.prices
        return round(statistics.median(p), 2) if p else None

    @property
    def average(self) -> Optional[float]:
        p = self.prices
        return round(statistics.mean(p), 2) if p else None

    @property
    def low(self) -> Optional[float]:
        p = self.prices
        return min(p) if p else None

    @property
    def high(self) -> Optional[float]:
        p = self.prices
        return max(p) if p else None

    @property
    def image_url(self) -> Optional[str]:
        for s in self.sales:
            if s.get("_image"):
                return s["_image"]
        return None

    @property
    def by_grade(self) -> dict:
        """Group sales by grade, return stats per grade."""
        groups = {}
        for s in self.sales:
            g = s.get("grade", "Raw")
            groups.setdefault(g, []).append(s["price"])
        result = {}
        for g, prices in sorted(groups.items()):
            result[g] = {
                "count":  len(prices),
                "median": round(statistics.median(prices), 2),
                "low":    min(prices),
                "high":   max(prices),
            }
        return result

    def to_dict(self) -> dict:
        return {
            "count":    self.count,
            "median":   self.median,
            "average":  self.average,
            "low":      self.low,
            "high":     self.high,
            "by_grade": self.by_grade,
            "sales":    self.sales[:50],
        }


class EbayMonitor:

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def search_sold(self, query: str, max_results: int = 50,
                          exclude_graded: bool = True) -> EbaySoldResult:
        """
        Search eBay sold listings for the given query.
        Returns an EbaySoldResult with up to max_results recent sales.
        """
        raw_items = await self._fetch_sold(query, max_results)

        filtered = []
        for item in raw_items:
            title_lower = item["title"].lower()
            # Always exclude lots/bundles/repacks/facsimiles
            if any(k in title_lower for k in ("lot of", "bundle", "repack", "facsimile")):
                continue
            # Only exclude graded if not searching for graded specifically
            if exclude_graded and any(k in title_lower for k in ("psa ", "bgs ", "cgc ", "graded")):
                continue
            filtered.append(item)

        return EbaySoldResult(filtered[:max_results])

    async def check_single(self, product: dict, stealth_cfg: dict | None = None) -> EbaySoldResult:
        """
        Build a search query from product tags and search eBay.
        Works for both MTG singles and comics.
        """
        tags = product.get("tags", {})
        name = product.get("name", "")
        category = tags.get("category", "single")

        parts = [name]

        if category == "comic":
            # Comics: broad search query, rely on post-filter for precision
            name_lower = name.lower()
            if tags.get("set") and tags["set"].lower() not in name_lower:
                parts.append(tags["set"])
            if tags.get("issue") and f"#{tags['issue']}" not in name:
                parts.append(f"#{tags['issue']}")
            if tags.get("variant") and tags["variant"].lower() not in name_lower:
                parts.append(tags["variant"])
            if tags.get("artist"):
                # Use last name only for broader search matches
                artist_parts = tags["artist"].split()
                last_name = artist_parts[-1] if artist_parts else ""
                if last_name.lower() not in name_lower:
                    parts.append(last_name)
        else:
            # MTG singles: include set and foil
            if tags.get("set") and tags["set"].lower() not in name.lower():
                parts.append(tags["set"])
            if tags.get("printing") and tags["printing"].lower() == "foil":
                parts.append("foil")

        query = " ".join(parts)
        log.info(f"eBay sold search: {query[:60]}")

        if category == "comic":
            # Use 130point.com for comics — better data, more marketplaces
            raw_sold, raw_live = await self._fetch_130point(query, max_results=50)

            # Post-filter both lists by key terms
            must_match = []
            if tags.get("variant"):
                must_match.extend(w.lower() for w in tags["variant"].split() if len(w) >= 3)
            if tags.get("artist"):
                artist_parts = tags["artist"].split()
                if artist_parts:
                    must_match.append(artist_parts[-1].lower())

            def _filter(items):
                out = items
                if must_match:
                    min_m = max(1, len(must_match) - 1)
                    out = [s for s in out
                           if sum(1 for t in must_match if t in s["title"].lower()) >= min_m]
                return [s for s in out
                        if not any(k in s["title"].lower() for k in ("lot of", "bundle", "repack", "facsimile"))]

            filtered_sold = _filter(raw_sold)
            filtered_live = _filter(raw_live)
            log.debug(f"130point post-filter: {len(filtered_sold)} sold, {len(filtered_live)} live")

            return EbaySoldResult(filtered_sold, filtered_live)

        # MTG singles: use direct eBay scrape
        exclude_graded = True
        result = await self.search_sold(query, exclude_graded=exclude_graded)
        return result

    # ------------------------------------------------------------------
    # Grade classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_grade(title: str) -> str:
        """
        Classify an eBay listing title as graded or raw.
        Returns e.g. "PSA 10", "CGC 9.8", or "Raw".
        """
        t = title.upper()

        # PSA grades — "PSA 9.8" or "PSA 10"
        m = re.search(r'PSA\s*(\d+\.?\d?)', t)
        if m:
            return f"PSA {m.group(1)}"

        # CGC grades — "CGC 9.8" or "CGC JSA 9.8" or "CGC SS 9.8"
        m = re.search(r'CGC[\s\w]*?(\d+\.?\d)', t)
        if m:
            return f"CGC {m.group(1)}"

        # CBCS grades — "CBCS 9.8" or "CBCS VF 9.8"
        m = re.search(r'CBCS[\s\w]*?(\d+\.?\d)', t)
        if m:
            return f"CBCS {m.group(1)}"

        # Bare grade number without a service (e.g. "D 9.8 Campbell")
        m = re.search(r'\b(10\.0|10|9\.\d|8\.\d|7\.\d)\b', t)
        if m:
            return f"Graded {m.group(1)}"

        if "GRADED" in t:
            return "Graded"

        return "Raw"

    # ------------------------------------------------------------------
    # 130point.com scraper (comics — replaces direct eBay for better data)
    # ------------------------------------------------------------------

    async def _fetch_130point(self, query: str, max_results: int) -> tuple[list[dict], list[dict]]:
        """
        Search 130point.com for sold AND live comic listings.
        Requires headless=False due to Cloudflare protection.
        Returns (sold_items, live_items) — each [{title, price, grade}].
        """
        try:
            from patchright.async_api import async_playwright as _pw
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _pw
            except ImportError:
                log.warning("130point: no playwright available")
                return []

        sold_items = []
        live_items = []
        try:
            import asyncio as _aio
            async with _pw() as pw:
                browser = None
                for channel in ("chrome", "msedge", None):
                    try:
                        kw = {"headless": False}
                        if channel:
                            kw["channel"] = channel
                        browser = await pw.chromium.launch(**kw)
                        break
                    except Exception:
                        continue
                if not browser:
                    return [], []

                page = await browser.new_page(viewport={"width": 1280, "height": 800})

                await page.goto(
                    "https://130point.com/comics/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                # Wait for Cloudflare challenge to resolve
                for _ in range(15):
                    await _aio.sleep(2)
                    title = await page.evaluate("() => document.title")
                    if "moment" not in title.lower():
                        break
                await _aio.sleep(2)

                # Search
                try:
                    await page.fill("input[placeholder*='Search']", query)
                    await page.keyboard.press("Enter")
                    await _aio.sleep(6)
                except Exception as e:
                    log.warning(f"130point search failed: {e}")
                    await browser.close()
                    return [], []

                # Grab the first listing image
                first_image = await page.evaluate("""() => {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        const src = img.src || '';
                        if (src.includes('ebayimg') && img.width > 50) return src;
                    }
                    return null;
                }""")

                # --- Sold tab ---
                try:
                    sold_btn = page.locator("button:has-text('Sold'), [role='tab']:has-text('Sold')")
                    if await sold_btn.first.is_visible(timeout=3000):
                        await sold_btn.first.click()
                        await _aio.sleep(3)
                except Exception:
                    pass

                text = await page.evaluate("() => document.body.innerText")
                sold_items = self._parse_130point_text(text, max_results)

                # Extract actual eBay image URL from 130point's proxy URL
                if first_image:
                    from urllib.parse import unquote
                    if "url=" in first_image:
                        inner = first_image.split("url=")[1].split("&")[0]
                        first_image = unquote(inner)
                    if sold_items:
                        sold_items[0]["_image"] = first_image

                # Live tab skipped — starting bid prices are misleading

                await browser.close()

        except Exception as e:
            log.warning(f"130point scrape error: {e}")

        log.debug(f"130point: {len(sold_items)} sold, {len(live_items)} live")
        return sold_items, live_items

    def _parse_130point_text(self, text: str, max_results: int) -> list[dict]:
        """Parse the raw page text from 130point into structured sale records."""
        lines = text.split("\n")
        items = []
        current_title = ""
        current_date = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Capture "Sold Feb 28, 2026" date lines
            date_match = re.match(r"Sold\s+(\w+ \d+,?\s*\d{0,4})", line)
            if date_match:
                current_date = date_match.group(1).strip()
                continue

            price_match = re.search(r"\$([\d,]+\.?\d{0,2})\s*USD", line)
            if price_match and current_title:
                price_str = price_match.group(1).replace(",", "")
                try:
                    price = float(price_str)
                except ValueError:
                    continue

                if 0.5 < price < 100_000:
                    items.append({
                        "title": current_title[:100],
                        "price": price,
                        "condition": "",
                        "grade": self._classify_grade(current_title),
                        "soldDate": current_date,
                    })
                    current_date = ""
                    if len(items) >= max_results:
                        break
                current_title = ""
            elif len(line) > 15 and "Sort" not in line and "Marketplace" not in line and "Apply" not in line and "Opens in a new" not in line:
                # Skip UI elements
                if not any(skip in line for skip in (
                    "Most Recent", "Price:", "Currency", "Buying format",
                    "Item location", "Sold date", "Clear All", "Similar Items",
                    "Release Calendar", "Find your"
                )):
                    current_title = line

        return items

    # ------------------------------------------------------------------
    # Playwright scraper (eBay direct — used for MTG singles)
    # ------------------------------------------------------------------

    async def _fetch_sold(self, query: str, max_results: int) -> list[dict]:
        """
        Navigate to eBay sold listings, wait for results to render,
        extract title + price + condition from the DOM.
        """
        try:
            from patchright.async_api import async_playwright as _pw
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _pw
            except ImportError:
                log.warning("eBay: no playwright available")
                return []

        from urllib.parse import quote
        encoded = quote(query)
        url = (
            f"https://www.ebay.com/sch/i.html?_nkw={encoded}"
            f"&LH_Complete=1&LH_Sold=1&_sop=13"  # sort by most recent
        )

        items = []
        try:
            import asyncio as _aio
            async with _pw() as pw:
                browser = None
                for channel in ("chrome", "msedge", None):
                    try:
                        kw = {"headless": True}
                        if channel:
                            kw["channel"] = channel
                        browser = await pw.chromium.launch(**kw)
                        break
                    except Exception:
                        continue
                if not browser:
                    return []

                page = await browser.new_page(viewport={"width": 1280, "height": 800})

                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await _aio.sleep(4)

                raw = await page.evaluate(r'''(maxResults) => {
                    const items = [];

                    // eBay uses .srp-results li containers
                    const cards = document.querySelectorAll(
                        ".srp-results .s-item, " +
                        "[data-testid='srp-river-results'] li, " +
                        ".srp-river-results li"
                    );

                    cards.forEach(card => {
                        if (items.length >= maxResults) return;

                        const titleEl = card.querySelector(
                            '.s-item__title span, [role="heading"], .s-card__title span'
                        );
                        const priceEl = card.querySelector(
                            '.s-item__price, .s-card__price'
                        );
                        const condEl = card.querySelector(
                            '.s-item__title--tag span, .s-card__subtitle, .POSITIVE'
                        );

                        const title = titleEl ? titleEl.textContent.trim() : '';
                        const priceText = priceEl ? priceEl.textContent.trim() : '';
                        const condition = condEl ? condEl.textContent.trim() : '';

                        if (!title || !priceText || title.includes('Shop on eBay'))
                            return;

                        // Parse price — handle "$12.02" and "$12.02 to $15.00" (use first)
                        const priceMatch = priceText.match(/\$([\d,]+\.?\d*)/);
                        const price = priceMatch ? parseFloat(priceMatch[1].replace(',', '')) : null;

                        // Check for Best Offer and sold date
                    const fullText = card.textContent || '';
                    const bestOffer = fullText.includes('Best offer') || fullText.includes('best offer');
                    const dateMatch = fullText.match(/Sold\s+(\w+ \d+,?\s*\d{0,4})/i);
                    const soldDate = dateMatch ? dateMatch[1].trim() : '';

                    if (price && price > 0.5 && price < 50000) {
                            const cleanTitle = title.replace(/Opens in a new window or tab/gi, '').trim();
                            items.push({ title: cleanTitle.substring(0, 100), price, condition, bestOffer, soldDate });
                        }
                    });

                    return items;
                }''', max_results)

                items = raw or []
                await browser.close()

        except Exception as e:
            log.warning(f"eBay scrape error: {e}")

        log.debug(f"eBay: {len(items)} sold listings found")
        return items
