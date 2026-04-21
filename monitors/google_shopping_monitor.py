"""
Google Shopping monitor.

Searches Google Shopping for watchlist products, clicks into the product
detail view, and extracts the full retailer comparison list (multiple
stores with prices, shipping, and links).

Uses Patchright/Playwright browser automation. Rate limited aggressively
to avoid CAPTCHAs — one search at a time with configurable delays.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from monitors.defaults import get_user_agent, get_browser_channel

log = logging.getLogger("google_shopping")

# Domains to flag as "major retailer" (already monitored via Discord)
MAJOR_DOMAINS = {
    "amazon.com", "amazon.ca", "walmart.com", "target.com",
    "bestbuy.com", "tcgplayer.com", "gamestop.com", "ebay.com",
    "barnesandnoble.com",
}


@dataclass
class ShoppingResult:
    title: str
    price: Optional[float]
    shipping: Optional[float]
    total: Optional[float]
    retailer: str
    domain: str
    url: str
    image_url: Optional[str] = None
    is_major: bool = False


class GoogleShoppingMonitor:

    def __init__(self):
        self._backoff_multiplier = 1.0

    def build_query(self, product: dict) -> str:
        """Build a Google Shopping search query from product info."""
        name = product.get("name", "")
        tags = product.get("tags", {})
        parts = [name]
        set_name = tags.get("set", "")
        if set_name and set_name.lower() not in name.lower():
            parts.append(set_name)
        query = " ".join(parts)
        query = re.sub(r'[—–]', '-', query)
        query = re.sub(r'[^\w\s\-\':.]', '', query)
        return query.strip()

    async def search(
        self,
        query: str,
        stealth_cfg: dict | None = None,
        excluded_domains: list[str] | None = None,
    ) -> list[ShoppingResult]:
        """Search Google Shopping, click into the product, and extract retailer comparison."""
        try:
            from patchright.async_api import async_playwright as _playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _playwright
            except ImportError:
                log.warning("Google Shopping: neither patchright nor playwright installed")
                return []

        excluded = set(excluded_domains or [])
        results = []

        try:
            async with _playwright() as pw:
                launch_kw = {"headless": True}
                channel = get_browser_channel(stealth_cfg)
                if channel:
                    launch_kw["channel"] = channel
                browser = await pw.chromium.launch(**launch_kw)
                context = await browser.new_context(
                    user_agent=get_user_agent(stealth_cfg),
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()

                # Search Google Shopping
                url = f"https://www.google.com/search?q={query}&udm=28"
                await page.goto(url, timeout=30000)
                await asyncio.sleep(3 + self._backoff_multiplier)

                # Dismiss cookie consent
                try:
                    consent = await page.query_selector(
                        'button[id*="agree" i], button[aria-label*="Accept" i], '
                        '[class*="consent"] button, button:has-text("Accept all")'
                    )
                    if consent:
                        await consent.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                # Check for CAPTCHA
                is_captcha = await page.evaluate("""() => {
                    return !!(
                        document.querySelector('#captcha-form') ||
                        document.querySelector('[id*="recaptcha"]') ||
                        document.title.toLowerCase().includes('unusual traffic') ||
                        document.title.toLowerCase().includes('sorry')
                    );
                }""")
                if is_captcha:
                    log.warning(f"Google Shopping: CAPTCHA for '{query[:40]}'")
                    self._backoff_multiplier = min(self._backoff_multiplier * 2, 8)
                    await browser.close()
                    return []

                self._backoff_multiplier = max(self._backoff_multiplier * 0.5, 1.0)

                # Save debug screenshot of search results
                try:
                    await page.screenshot(path="google_shopping_search.png")
                    log.info("Screenshot saved: google_shopping_search.png")
                except Exception:
                    pass

                # First: extract all product cards from the search results page
                # Each card shows one retailer with price
                raw = await page.evaluate("""() => {
                    const results = [];

                    // Google Shopping grid cards — each card is a product+retailer pair
                    // The grid contains divs with product info
                    const allLinks = document.querySelectorAll('a[href]');
                    const processed = new Set();

                    for (const a of allLinks) {
                        // Find the card container (usually a few levels up)
                        let card = a;
                        for (let i = 0; i < 5; i++) {
                            if (!card.parentElement) break;
                            card = card.parentElement;
                            // Stop if we hit a grid-level container
                            if (card.children.length > 5) break;
                        }

                        if (processed.has(card)) continue;

                        const text = card.textContent || '';
                        // Must have a price
                        const priceMatch = text.match(/\\$(\\d[\\d,.]+\\.\\d{2})/);
                        if (!priceMatch) continue;

                        // Must have an image (product card indicator)
                        const img = card.querySelector('img[src*="http"]');
                        if (!img) continue;

                        processed.add(card);

                        // Extract title — usually in a heading or prominent text
                        let title = '';
                        const h3 = card.querySelector('h3, [role="heading"]');
                        if (h3) title = h3.textContent.trim();
                        if (!title) {
                            const titleEl = card.querySelector('div[style*="font-weight"] span, div > span');
                            if (titleEl) title = titleEl.textContent.trim();
                        }

                        // Extract retailer — typically in a smaller text near the price
                        // Look for short text that looks like a store name
                        let retailer = '';
                        const spans = card.querySelectorAll('span, div');
                        for (const el of spans) {
                            const t = el.textContent.trim();
                            // Retailer heuristic: short text, not a price, not the title,
                            // not common labels, usually near bottom of card
                            if (t && t.length >= 3 && t.length <= 40 &&
                                !t.includes('$') && !t.includes('★') &&
                                t !== title && !t.startsWith(title) &&
                                !t.match(/^(free|\\d|rating|sale|new|ad|foil|non-foil|sealed|near mint|sponsored|browse|sort)/i) &&
                                !t.match(/^(magic|pokemon|yu-gi-oh|the |mtg |secrets|collector|booster)/i) &&
                                !t.match(/(day|delivery|shipping|arrive|pre-owned|condition)/i) &&
                                el.children.length === 0) {
                                // Likely a store name
                                retailer = t;
                            }
                        }

                        // Shipping
                        let shipping = '';
                        const textLower = text.toLowerCase();
                        if (textLower.includes('free delivery') || textLower.includes('free shipping')) {
                            shipping = 'free';
                        } else {
                            const sm = text.match(/delivery\\s*\\$(\\d[\\d.]*)/i) || text.match(/\\+(\\$[\\d.]+)/);
                            if (sm) shipping = sm[1];
                        }

                        const url = a.href;

                        if (retailer && priceMatch) {
                            results.push({
                                title: title.substring(0, 200),
                                price: '$' + priceMatch[1],
                                retailer: retailer,
                                url: url,
                                shipping: shipping,
                                image: img ? img.src : '',
                            });
                        }
                    }

                    return { results, count: results.length };
                }""")

                search_results = raw.get("results", []) if raw else []
                log.info(f"Google Shopping: '{query[:40]}' — {len(search_results)} results from search page")

                # Second: try to click into a product for the full retailer comparison
                try:
                    # Click the first product image/card
                    first_card = await page.query_selector(
                        'a[href*="/shopping/product/"], '
                        'a[href*="google.com/url"], '
                        'div[role="listitem"] a'
                    )
                    if not first_card:
                        # Try clicking the first product image
                        first_card = await page.query_selector('img[src*="encrypted"]')
                        if first_card:
                            first_card = await first_card.evaluate_handle("el => el.closest('a') || el")

                    if first_card:
                        await first_card.click()
                        await asyncio.sleep(3)

                        # Save screenshot of detail page
                        try:
                            await page.screenshot(path="google_shopping_detail.png")
                        except Exception:
                            pass

                        # Extract retailers from detail/comparison panel
                        detail_raw = await page.evaluate("""() => {
                            const results = [];
                            // Look for offer/merchant rows in the comparison panel
                            const containers = document.querySelectorAll(
                                '[class*="offer"], [class*="merchant"], ' +
                                '[class*="seller"], [data-merchant-id]'
                            );
                            for (const el of containers) {
                                const text = el.textContent || '';
                                const pm = text.match(/\\$(\\d[\\d,.]+\\.\\d{2})/);
                                if (!pm) continue;
                                // Find store name
                                let store = '';
                                for (const s of el.querySelectorAll('span, a, div')) {
                                    const t = s.textContent.trim();
                                    if (t && t.length >= 3 && t.length <= 40 &&
                                        !t.includes('$') && !t.includes('★') &&
                                        !t.match(/^(free|\\d|total|buy|add|compare|visit|view)/i) &&
                                        s.children.length === 0) {
                                        store = t;
                                        break;
                                    }
                                }
                                const link = el.querySelector('a[href]');
                                if (store && pm) {
                                    let shipping = '';
                                    if (text.toLowerCase().includes('free')) shipping = 'free';
                                    results.push({
                                        title: '',
                                        price: '$' + pm[1],
                                        retailer: store,
                                        url: link ? link.href : '',
                                        shipping,
                                    });
                                }
                            }
                            return results;
                        }""")

                        if detail_raw and len(detail_raw) > len(search_results):
                            log.info(f"Google Shopping: detail panel had {len(detail_raw)} retailers")
                            search_results = detail_raw
                except Exception as e:
                    log.debug(f"Google Shopping: detail click failed: {e}")

                await browser.close()

                raw_results = search_results

                # Parse results
                seen_domains = set()
                for r in raw_results:
                    price = self._parse_price(r.get("price", ""))
                    shipping = self._parse_shipping(r.get("shipping", ""))
                    total = None
                    if price is not None:
                        total = price + (shipping if shipping and shipping > 0 else 0)

                    retailer = r.get("retailer", "").strip()
                    url = r.get("url", "")
                    domain = self._extract_domain(url)

                    # Deduplicate by domain
                    if domain in seen_domains:
                        continue
                    seen_domains.add(domain)

                    is_major = self._is_major_retailer(domain, excluded)

                    if retailer and price:
                        results.append(ShoppingResult(
                            title=r.get("title", "") or query,
                            price=price,
                            shipping=shipping,
                            total=total,
                            retailer=retailer,
                            domain=domain,
                            url=url,
                            image_url=r.get("image") or None,
                            is_major=is_major,
                        ))

                return results

        except Exception as e:
            log.warning(f"Google Shopping search error: {e}")
            return []

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(r'\$\s*([\d,]+\.?\d*)', text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_shipping(text: str) -> Optional[float]:
        if not text:
            return None
        text = text.lower().strip()
        if "free" in text:
            return 0.0
        m = re.search(r'\$?\s*([\d.]+)', text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if host.startswith("www."):
                host = host[4:]
            # Google redirect URLs: extract actual domain
            if "google.com" in host:
                m = re.search(r'url=(https?://[^&]+)', url)
                if m:
                    return GoogleShoppingMonitor._extract_domain(m.group(1))
                # Try extracting from q= parameter
                m = re.search(r'[?&]q=(https?://[^&]+)', url)
                if m:
                    return GoogleShoppingMonitor._extract_domain(m.group(1))
            return host
        except Exception:
            return ""

    @staticmethod
    def _is_major_retailer(domain: str, excluded: set[str] | None = None) -> bool:
        if not domain:
            return False
        all_excluded = MAJOR_DOMAINS | (excluded or set())
        return any(domain.endswith(d) or d.endswith(domain) for d in all_excluded)
