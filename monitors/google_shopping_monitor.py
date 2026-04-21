"""
Google Shopping monitor.

Searches Google Shopping for watchlist products to find deals from
smaller/indie retailers not covered by the Discord deal feeds.

Uses Patchright/Playwright browser automation. Rate limited aggressively
to avoid CAPTCHAs — one search at a time with configurable delays.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
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
        self._backoff_multiplier = 1.0  # Increases on CAPTCHA

    def build_query(self, product: dict) -> str:
        """Build a Google Shopping search query from product info."""
        name = product.get("name", "")
        tags = product.get("tags", {})

        parts = [name]
        # Add set name if not already in the product name
        set_name = tags.get("set", "")
        if set_name and set_name.lower() not in name.lower():
            parts.append(set_name)

        query = " ".join(parts)
        # Clean up special characters that confuse Google
        query = re.sub(r'[—–]', '-', query)
        query = re.sub(r'[^\w\s\-\':.]', '', query)
        return query.strip()

    async def search(
        self,
        query: str,
        stealth_cfg: dict | None = None,
        excluded_domains: list[str] | None = None,
    ) -> list[ShoppingResult]:
        """Search Google Shopping and return results."""
        try:
            from patchright.async_api import async_playwright as _playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _playwright
            except ImportError:
                log.warning("Google Shopping: neither patchright nor playwright installed")
                return []

        results = []
        excluded = set(excluded_domains or [])

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

                url = f"https://www.google.com/search?q={query}&tbm=shop&hl=en"
                await page.goto(url, timeout=30000)

                # Wait for results to render
                await asyncio.sleep(3 + (2 * self._backoff_multiplier))

                # Dismiss cookie consent if present
                try:
                    consent_btn = await page.query_selector(
                        'button[id*="agree"], button[aria-label*="Accept"], '
                        '[class*="consent"] button'
                    )
                    if consent_btn:
                        await consent_btn.click()
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
                    log.warning(f"Google Shopping: CAPTCHA detected for query '{query[:40]}'")
                    self._backoff_multiplier = min(self._backoff_multiplier * 2, 8)
                    await browser.close()
                    return []

                # Successful search — reset backoff
                self._backoff_multiplier = max(self._backoff_multiplier * 0.5, 1.0)

                # Scroll down to load more results
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(1)

                # Extract shopping results from DOM
                raw_results = await page.evaluate("""() => {
                    const results = [];

                    // Google Shopping result cards — try multiple selector patterns
                    const selectors = [
                        '.sh-dgr__grid-result',
                        '.sh-dgr__content',
                        '[data-docid]',
                        '.sh-dlr__list-result',
                        '.pla-unit',
                    ];

                    let cards = [];
                    for (const sel of selectors) {
                        cards = document.querySelectorAll(sel);
                        if (cards.length > 0) break;
                    }

                    // Fallback: look for any structured result with price
                    if (!cards.length) {
                        cards = document.querySelectorAll('[class*="sh-"] a[href*="shopping"]');
                    }

                    for (const card of cards) {
                        try {
                            // Title
                            let titleEl = card.querySelector('h3, [class*="title"], [class*="name"], [aria-level]');
                            let title = titleEl ? titleEl.textContent.trim() : '';

                            // Price
                            let priceText = '';
                            let priceEls = card.querySelectorAll('[class*="price"], [class*="Price"], b, strong');
                            for (let pe of priceEls) {
                                let t = pe.textContent.trim();
                                if (t.match(/^\\$[\\d,.]+/)) {
                                    priceText = t;
                                    break;
                                }
                            }
                            // Fallback: search all text nodes for price
                            if (!priceText) {
                                let allText = card.textContent;
                                let m = allText.match(/\\$(\\d[\\d,.]*)/);
                                if (m) priceText = '$' + m[1];
                            }

                            // Retailer/store name
                            let retailerEl = card.querySelector(
                                '[class*="merchant"], [class*="store"], [class*="seller"], ' +
                                '[class*="aULzUe"], [data-merchant-id]'
                            );
                            let retailer = retailerEl ? retailerEl.textContent.trim() : '';
                            // Sometimes retailer is in a small text below the price
                            if (!retailer) {
                                let smallTexts = card.querySelectorAll('span, div');
                                for (let st of smallTexts) {
                                    let t = st.textContent.trim();
                                    if (t && !t.includes('$') && t.length < 40 && t.length > 2 &&
                                        !t.match(/^(Free|\\d|rating|star|review|ship)/i)) {
                                        // Heuristic: short text that isn't a price or rating
                                        if (!retailer || t.length < retailer.length) {
                                            retailer = t;
                                        }
                                    }
                                }
                            }

                            // URL
                            let linkEl = card.querySelector('a[href]');
                            let url = linkEl ? linkEl.href : '';

                            // Shipping
                            let shipping = '';
                            let shipEl = card.querySelector('[class*="shipping"], [class*="delivery"]');
                            if (shipEl) shipping = shipEl.textContent.trim();
                            // Check for "Free shipping" or "$X.XX shipping"
                            if (!shipping) {
                                let text = card.textContent.toLowerCase();
                                if (text.includes('free shipping') || text.includes('free delivery')) {
                                    shipping = 'free';
                                } else {
                                    let sm = text.match(/\\+(\\$[\\d.]+)\\s*(?:shipping|delivery)/);
                                    if (sm) shipping = sm[1];
                                }
                            }

                            // Image
                            let imgEl = card.querySelector('img[src]');
                            let image = imgEl ? imgEl.src : '';

                            if (title || priceText) {
                                results.push({
                                    title: title.substring(0, 200),
                                    price: priceText,
                                    retailer: retailer.substring(0, 100),
                                    url: url,
                                    shipping: shipping,
                                    image: image,
                                });
                            }
                        } catch(e) {}
                    }

                    return results;
                }""")

                await browser.close()

                # Parse and structure results
                for r in raw_results:
                    price = self._parse_price(r.get("price", ""))
                    shipping = self._parse_shipping(r.get("shipping", ""))
                    total = None
                    if price is not None:
                        total = price + (shipping if shipping and shipping > 0 else 0)

                    retailer = r.get("retailer", "").strip()
                    url = r.get("url", "")
                    domain = self._extract_domain(url)

                    is_major = self._is_major_retailer(domain, excluded)

                    if retailer and price:
                        results.append(ShoppingResult(
                            title=r.get("title", ""),
                            price=price,
                            shipping=shipping,
                            total=total,
                            retailer=retailer,
                            domain=domain,
                            url=url,
                            image_url=r.get("image") or None,
                            is_major=is_major,
                        ))

                log.info(f"Google Shopping: '{query[:40]}' — {len(results)} results ({sum(1 for r in results if not r.is_major)} indie)")
                return results

        except Exception as e:
            log.warning(f"Google Shopping search error: {e}")
            return []

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        """Parse a price string like '$29.99' into a float."""
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
        """Parse shipping cost. Returns 0 for free, float for cost, None for unknown."""
        if not text:
            return None
        text = text.lower().strip()
        if "free" in text:
            return 0.0
        m = re.search(r'\$\s*([\d.]+)', text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract the base domain from a URL."""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            # Strip www. prefix
            if host.startswith("www."):
                host = host[4:]
            # For Google redirect URLs, try to extract the actual domain
            if "google.com" in host and "url=" in url:
                m = re.search(r'url=(https?://[^&]+)', url)
                if m:
                    return GoogleShoppingMonitor._extract_domain(m.group(1))
            return host
        except Exception:
            return ""

    @staticmethod
    def _is_major_retailer(domain: str, excluded: set[str] | None = None) -> bool:
        """Check if a domain is a major retailer."""
        if not domain:
            return False
        all_excluded = MAJOR_DOMAINS | (excluded or set())
        return any(domain.endswith(d) or d.endswith(domain) for d in all_excluded)
