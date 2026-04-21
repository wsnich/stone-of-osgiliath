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

                print(f"  [Shopping] Page loaded: {page.url[:60]}")

                # Save debug screenshot + HTML for selector analysis
                try:
                    await page.screenshot(path="google_shopping_search.png")
                    html = await page.content()
                    with open("google_shopping_page.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    log.info("Saved: google_shopping_search.png + google_shopping_page.html")
                except Exception:
                    pass

                # Diagnostic: dump first 10 links with their text
                diag = await page.evaluate("""() => {
                    const links = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        const text = a.textContent.trim().substring(0, 100);
                        const href = a.href.substring(0, 80);
                        if (text.includes('$') && text.length > 10) {
                            links.push({ text, href });
                        }
                    }
                    return links.slice(0, 10);
                }""")
                for i, l in enumerate(diag[:5]):
                    print(f"  [Shopping] Link {i}: text='{l.get('text', '')[:80]}' href={l.get('href', '')[:60]}")

                # Extract product cards from Google Shopping results
                raw = await page.evaluate("""() => {
                    const results = [];
                    const debug = [];

                    // Find all links — use broad selector, filter by content
                    const anchors = document.querySelectorAll('a[href]');
                    const processed = new Set();

                    for (const a of anchors) {
                        const url = a.href;
                        if (processed.has(url)) continue;
                        // Skip Google internal navigation links
                        if (url.includes('google.com/search') || url.includes('accounts.google') ||
                            url.includes('support.google') || url.includes('policies.google') ||
                            url.startsWith('#') || url.includes('/preferences') ||
                            url.includes('maps.google')) continue;

                        // Walk up to find a container with product info
                        let card = a;
                        for (let i = 0; i < 8; i++) {
                            if (!card.parentElement) break;
                            // Stop if we hit a very large container
                            if (card.parentElement.querySelectorAll('a[href]').length > 10) break;
                            card = card.parentElement;
                        }

                        const text = card.textContent || '';
                        // Must have a price
                        const priceMatch = text.match(/\\$(\\d[\\d,.]*\\.\\d{2})/);
                        if (!priceMatch) continue;

                        processed.add(url);

                        // Get leaf text nodes for structured parsing
                        const leaves = [];
                        function getLeaves(el) {
                            if (el.children.length === 0) {
                                const t = el.textContent.trim();
                                if (t && t.length > 1) leaves.push(t);
                            } else {
                                for (const c of el.children) getLeaves(c);
                            }
                        }
                        getLeaves(card);

                        // Parse card: find title, retailer, shipping from leaf nodes
                        let retailer = '';
                        let title = '';
                        let shipping = '';
                        let foundPrice = false;

                        for (const leaf of leaves) {
                            if (leaf.startsWith('$') || leaf.match(/^\\d+\\.\\d{2}$/)) {
                                foundPrice = true;
                                continue;
                            }
                            if (!foundPrice && leaf.length > 20 && !title) {
                                title = leaf;
                                continue;
                            }
                            if (foundPrice && !retailer &&
                                leaf.length >= 3 && leaf.length <= 50 &&
                                !leaf.startsWith('$') && !leaf.includes('★') &&
                                !leaf.match(/^(free|\\d|sponsored|sale|ad|more|compare|see|view|show|sort|filter|buy|visit)/i) &&
                                !leaf.match(/^(magic|pokemon|the |mtg |marvel|collector|booster|secrets)/i) &&
                                !leaf.match(/(delivery|shipping|day|pre-owned|condition|results)/i)) {
                                retailer = leaf;
                                continue;
                            }
                            if (foundPrice && !shipping) {
                                const ll = leaf.toLowerCase();
                                if (ll.includes('free delivery') || ll.includes('free shipping')) {
                                    shipping = 'free';
                                }
                            }
                        }

                        debug.push({
                            leaves: leaves.slice(0, 10),
                            retailer, title: (title || '').substring(0, 50),
                            url: url.substring(0, 60),
                        });

                        const img = card.querySelector('img[src*="http"]');
                        if (retailer) {
                            results.push({
                                title: (title || '').substring(0, 200),
                                price: '$' + priceMatch[1],
                                retailer,
                                url,
                                shipping,
                                image: img ? img.src : '',
                            });
                        }
                    }

                    return { results, debug: debug.slice(0, 20), count: results.length };
                }""")

                search_results = raw.get("results", []) if raw else []
                debug_cards = raw.get("debug", []) if raw else []
                print(f"  [Shopping] Extracted {len(search_results)} results, {len(debug_cards)} debug cards")

                # Log debug info for first few cards
                for i, d in enumerate(debug_cards[:5]):
                    print(f"  [Shopping] Card {i}: retailer='{d.get('retailer')}' leaves={d.get('leaves', [])[:5]}")

                await browser.close()

                raw_results = search_results

                # Parse and deduplicate results
                seen_retailers = set()
                for r in raw_results:
                    price = self._parse_price(r.get("price", ""))
                    shipping = self._parse_shipping(r.get("shipping", ""))
                    total = None
                    if price is not None:
                        total = price + (shipping if shipping and shipping > 0 else 0)

                    retailer = r.get("retailer", "").strip()
                    url = r.get("url", "")
                    domain = self._extract_domain(url)

                    # Deduplicate by retailer name (case-insensitive)
                    retailer_key = retailer.lower()
                    if retailer_key in seen_retailers:
                        continue
                    seen_retailers.add(retailer_key)

                    # Check if major retailer by domain OR by name
                    is_major = self._is_major_retailer(domain, excluded)
                    if not is_major:
                        major_names = {"tcgplayer", "amazon", "walmart", "target", "best buy",
                                       "bestbuy", "ebay", "gamestop", "barnes & noble"}
                        is_major = retailer.lower().replace(".com", "") in major_names

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
