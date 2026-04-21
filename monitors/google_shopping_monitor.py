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

                # Click the first product card to open the retailer comparison
                clicked = await page.evaluate("""() => {
                    // Find clickable product cards
                    let cards = document.querySelectorAll(
                        '[data-docid], .sh-dgr__grid-result, .sh-pr__product-results a'
                    );
                    if (!cards.length) {
                        // Try broader: any link to a shopping product detail
                        cards = document.querySelectorAll('a[href*="/shopping/product/"]');
                    }
                    if (cards.length > 0) {
                        cards[0].click();
                        return true;
                    }
                    return false;
                }""")

                if not clicked:
                    log.info(f"Google Shopping: no product cards found for '{query[:40]}'")
                    await browser.close()
                    return []

                # Wait for the retailer comparison panel/page to load
                await asyncio.sleep(3)

                # Extract retailer comparison data
                raw = await page.evaluate("""() => {
                    const results = [];
                    const productTitle = (
                        document.querySelector('h1, [class*="product-title"], [class*="BvQan"]')
                        || {}
                    ).textContent || '';

                    // Method 1: Merchant/offer rows in the comparison panel
                    // Google shows these as rows with store name, price, shipping, link
                    const selectors = [
                        // Comparison table rows
                        '[class*="sh-osd__offer"], [class*="merchant-offer"]',
                        // Shopping product offers
                        'tr[class*="offer"], [data-merchant-id]',
                        // Generic offer blocks
                        '[class*="sh-pr__offer"]',
                        // Fallback: any element with both a price and a store link
                        '.sh-osd__content > div',
                    ];

                    let offers = [];
                    for (const sel of selectors) {
                        offers = document.querySelectorAll(sel);
                        if (offers.length > 1) break;
                    }

                    // If no offer rows found, try parsing the full page more broadly
                    if (offers.length <= 1) {
                        // Look for structured price+store pairs
                        const allLinks = document.querySelectorAll('a[href*="url="]');
                        for (const a of allLinks) {
                            const container = a.closest('div, tr, li');
                            if (!container) continue;
                            const text = container.textContent;
                            const priceMatch = text.match(/\\$(\\d[\\d,.]*)/);
                            if (!priceMatch) continue;
                            // Store name: look for short text that isn't the price
                            let store = '';
                            for (const el of container.querySelectorAll('span, div, a')) {
                                const t = el.textContent.trim();
                                if (t && !t.includes('$') && t.length > 2 && t.length < 50 &&
                                    !t.match(/^(free|\\d|rating|review|ship|delivery|compare)/i)) {
                                    store = t;
                                    break;
                                }
                            }
                            if (store) {
                                let shipping = '';
                                if (text.toLowerCase().includes('free')) shipping = 'free';
                                else {
                                    const sm = text.match(/\\+(\\$[\\d.]+)/);
                                    if (sm) shipping = sm[1];
                                }
                                results.push({
                                    title: productTitle.trim(),
                                    price: '$' + priceMatch[1],
                                    retailer: store,
                                    url: a.href,
                                    shipping: shipping,
                                });
                            }
                        }
                        return { results, method: 'links' };
                    }

                    // Parse structured offer rows
                    for (const offer of offers) {
                        try {
                            const text = offer.textContent || '';

                            // Store/retailer name
                            let storeEl = offer.querySelector(
                                '[class*="merchant"], [class*="store"], [class*="seller"], ' +
                                'a[class*="merchant"], [class*="aULzUe"]'
                            );
                            let store = storeEl ? storeEl.textContent.trim() : '';
                            if (!store) {
                                // Try the first short text element
                                for (const el of offer.querySelectorAll('span, a, div')) {
                                    const t = el.textContent.trim();
                                    if (t && t.length > 2 && t.length < 50 && !t.includes('$') &&
                                        !t.match(/^(free|\\d|total|buy|add|compare|rating|star)/i)) {
                                        store = t;
                                        break;
                                    }
                                }
                            }

                            // Price
                            let priceMatch = text.match(/\\$(\\d[\\d,.]*)/);
                            let price = priceMatch ? '$' + priceMatch[1] : '';

                            // URL
                            let link = offer.querySelector('a[href]');
                            let url = link ? link.href : '';

                            // Shipping
                            let shipping = '';
                            let textLower = text.toLowerCase();
                            if (textLower.includes('free shipping') || textLower.includes('free delivery')) {
                                shipping = 'free';
                            } else {
                                let sm = text.match(/\\+(\\$[\\d.]+)\\s*(?:ship|deliver)/i);
                                if (!sm) sm = text.match(/shipping\\s*\\$(\\d[\\d.]*)/i);
                                if (sm) shipping = '$' + (sm[1].startsWith('$') ? sm[1].slice(1) : sm[1]);
                            }

                            // Image
                            let img = offer.querySelector('img[src]');
                            let image = img ? img.src : '';

                            if (store && price) {
                                results.push({
                                    title: productTitle.trim(),
                                    price, retailer: store, url, shipping, image,
                                });
                            }
                        } catch(e) {}
                    }

                    return { results, method: 'offers' };
                }""")

                await browser.close()

                raw_results = raw.get("results", []) if raw else []
                method = raw.get("method", "?")
                log.info(f"Google Shopping: '{query[:40]}' — {len(raw_results)} retailers (method: {method})")

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
