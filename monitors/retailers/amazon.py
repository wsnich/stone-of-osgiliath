"""
Amazon-specific browser automation.

Phase 1 implements only the login flow:
  - Launch Patchright through the account's pinned proxy
  - Open https://www.amazon.com/?language=en_US
  - If already logged in → save session and report ok
  - If not → either auto-fill email/password (when not headless and creds present)
    or just open the visible browser and let the user finish (handles 2FA / OTP)

Later phases will add ATC and checkout in this same module.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable, Awaitable

from monitors.account_manager import (
    Account, session_path_for,
    STATUS_OK, STATUS_AWAITING, STATUS_EXPIRED, STATUS_ERROR,
)

log = logging.getLogger("amazon")

LOGIN_URL = "https://www.amazon.com/ap/signin?_encoding=UTF8&openid.return_to=https%3A%2F%2Fwww.amazon.com%2F&openid.mode=checkid_setup&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
HOME_URL  = "https://www.amazon.com/?language=en_US"

# Chromium flags that prevent Windows Hello / Credential Manager from hijacking
# password / autofill flows during login. Kept minimal so we don't fingerprint
# as a bot (extra --disable-features flags can be detected by anti-bot systems).
_BROWSER_ARGS = [
    "--password-store=basic",
    "--disable-save-password-bubble",
]


def _proxy_to_playwright(proxy_str: str) -> Optional[dict]:
    """Convert host:port:user:pass into Playwright proxy dict."""
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) != 4:
        return None
    host, port, user, pw = parts
    return {"server": f"http://{host}:{port}", "username": user, "password": pw}


async def _is_logged_in(page) -> bool:
    """Check whether the current page indicates a logged-in Amazon session.
    Non-blocking: returns False fast on pages where the indicator isn't present
    (sign-in flow, captchas, OTP, etc.) instead of waiting around."""
    try:
        text = await page.evaluate("""() => {
            // The greeting in the header — present on home, account, search,
            // most product pages. Absent on /ap/signin and /ap/cvf and captchas.
            const el = document.querySelector('#nav-link-accountList-nav-line-1, #nav-link-accountList .nav-line-1');
            return el ? el.textContent.trim() : '';
        }""")
    except Exception:
        return False
    if not text:
        return False
    return "sign in" not in text.lower() and "hello, sign" not in text.lower()


async def login(account: Account,
                user_agent: str,
                browser_channel: Optional[str],
                on_status: Callable[[str, str], Awaitable[None]],
                login_timeout_sec: int = 300) -> bool:
    """
    Open a visible browser, navigate to Amazon, and either confirm a saved
    session is still valid OR wait up to login_timeout_sec for the user to log in.
    Returns True on success.

    on_status(status: str, message: str) is awaited whenever progress changes —
    used by the caller to broadcast WS updates.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            await on_status(STATUS_ERROR, "neither patchright nor playwright installed")
            return False

    proxy = _proxy_to_playwright(account.proxy)
    session_path = session_path_for(account.id)

    async with async_playwright() as pw:
        launch_kw = {"headless": False, "args": list(_BROWSER_ARGS)}
        if browser_channel:
            launch_kw["channel"] = browser_channel
        if proxy:
            launch_kw["proxy"] = proxy

        browser = await pw.chromium.launch(**launch_kw)
        context_kw = {
            "user_agent": user_agent,
            "viewport": {"width": 1280, "height": 900},
        }
        if session_path.exists():
            try:
                context_kw["storage_state"] = str(session_path)
                await on_status(STATUS_AWAITING, "Restoring saved Amazon session…")
            except Exception:
                pass

        ctx  = await browser.new_context(**context_kw)
        page = await ctx.new_page()
        try:
            await on_status(STATUS_AWAITING, f"Opening Amazon for '{account.name}'…")
            await page.goto(HOME_URL, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            if await _is_logged_in(page):
                await on_status(STATUS_OK, "Already logged in")
                await ctx.storage_state(path=str(session_path))
                return True

            # Need login. Stay on the home page and let the user click Sign In
            # in the header — Amazon's direct /ap/signin URL throws errors when
            # accessed without a proper redirect chain.
            await on_status(STATUS_AWAITING,
                f"*** Please click Sign In and log into Amazon as '{account.email}' "
                "in the browser window. The window will auto-close when login is detected. ***")

            # Poll for the logged-in state on whatever page they end up on
            last_url = ""
            for i in range(login_timeout_sec):
                await asyncio.sleep(1)
                try:
                    url = page.url
                    if url != last_url:
                        last_url = url
                    if await _is_logged_in(page):
                        await on_status(STATUS_OK, f"Logged in as '{account.name}'")
                        await ctx.storage_state(path=str(session_path))
                        return True
                except Exception:
                    pass

            await on_status(STATUS_ERROR, "Login timed out (5 minutes)")
            return False
        finally:
            try: await ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass


def url_from_asin(asin: str) -> str:
    return f"https://www.amazon.com/dp/{asin}?th=1&psc=1"


async def add_to_cart(account: Account,
                      url: str,
                      user_agent: str,
                      browser_channel: Optional[str],
                      headless: bool = True,
                      quantity: int = 1) -> dict:
    """Open the product page through this account's saved session + pinned proxy
    and click Add to Cart. Returns {success: bool, message: str, cart_count: int|None}.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "message": "neither patchright nor playwright installed", "cart_count": None}

    session_path = session_path_for(account.id)
    if not session_path.exists():
        return {"success": False, "message": "No saved session — log in first", "cart_count": None}

    proxy = _proxy_to_playwright(account.proxy)

    async with async_playwright() as pw:
        launch_kw = {"headless": headless, "args": list(_BROWSER_ARGS)}
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kw)
        try:
            ctx = await browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 900},
                storage_state=str(session_path),
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                return {"success": False, "message": f"Failed to load product: {e}", "cart_count": None}

            # If we ended up on a sign-in page, the session is stale
            if "/ap/signin" in page.url:
                return {"success": False, "message": "Session expired — re-login this account", "cart_count": None}

            # Set quantity if a quantity dropdown is present (best-effort)
            if quantity > 1:
                try:
                    await page.select_option("select#quantity", str(quantity), timeout=2500)
                except Exception:
                    pass

            # Try a sequence of known Add-to-Cart selectors. Amazon varies the
            # button across product types (single, variation, used/new, etc.).
            atc_selectors = [
                "#add-to-cart-button",
                "input#add-to-cart-button",
                "input[name='submit.add-to-cart']",
                "[data-feature-id='desktop_buybox'] input[name='submit.add-to-cart']",
                "[data-action='add-to-cart'] input[type='submit']",
                "input[aria-labelledby='submit.add-to-cart-announce']",
            ]
            clicked = False
            for sel in atc_selectors:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=3000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                # Multi-seller offer flow — common for collectibles like booster boxes.
                # The product page shows "See All Buying Options" instead of a direct
                # ATC button. We follow the offer listing and click ATC there.
                offer_link_selectors = [
                    "#buybox-see-all-buying-choices a",
                    "a[href*='/gp/offer-listing/']",
                    "#newOfferAccordionRow a",
                    "#availability a[href*='offer-listing']",
                    "a[aria-label*='New & used'], a[aria-label*='all buying options']",
                ]
                offer_url = None
                for sel in offer_link_selectors:
                    try:
                        el = page.locator(sel).first
                        await el.wait_for(state="visible", timeout=2000)
                        href = await el.get_attribute("href")
                        if href:
                            offer_url = href if href.startswith("http") else f"https://www.amazon.com{href}"
                            break
                    except Exception:
                        continue

                if offer_url:
                    try:
                        await page.goto(offer_url, timeout=30000, wait_until="domcontentloaded")
                        await asyncio.sleep(2)
                    except Exception as e:
                        return {"success": False, "message": f"Couldn't open offer listing: {e}", "cart_count": None}

                    # On the offer-listing page, the FIRST seller is the buy-box winner.
                    # Click its Add-to-Cart button.
                    offer_atc_selectors = [
                        "input[name='submit.addToCart']",
                        "[data-action='aod-atc-action'] input[type='submit']",
                        "#aod-pinned-offer input[name='submit.addToCart']",
                        ".aod-atc-action input[type='submit']",
                        "#aod-offer-list input[name='submit.addToCart']",
                    ]
                    for sel in offer_atc_selectors:
                        try:
                            el = page.locator(sel).first
                            await el.wait_for(state="visible", timeout=3000)
                            await el.click()
                            clicked = True
                            break
                        except Exception:
                            continue

                if not clicked:
                    # Detect why we couldn't ATC
                    diag = await page.evaluate(r"""() => {
                        const t = (document.body.innerText || '').toLowerCase();
                        if (t.includes('captcha') || t.includes('robot')) return 'captcha';
                        if (t.includes('currently unavailable') || t.includes('out of stock')) return 'oos';
                        if (document.querySelector('#availability') &&
                            /unavailable|out of stock/.test(document.querySelector('#availability').innerText.toLowerCase())) return 'oos';
                        if (t.includes('see all buying options')) return 'multi-seller';
                        if (document.querySelector('select#native_dropdown_selected_size_name, #variation_size_name'))
                            return 'variation';
                        return 'unknown';
                    }""")
                    reason_map = {
                        'captcha':       "CAPTCHA — page is challenging the session",
                        'oos':           "Out of stock",
                        'multi-seller':  "Multi-seller offer — couldn't follow the offer listing",
                        'variation':     "Variation/size picker required (select condition or printing)",
                        'unknown':       "Could not find Add to Cart button (page layout unrecognized)",
                    }
                    return {"success": False, "message": reason_map.get(diag, reason_map['unknown']), "cart_count": None}

            # Wait for either the "added to cart" landing or the cart count to update
            await asyncio.sleep(2)
            cart_count: Optional[int] = None
            try:
                txt = await page.evaluate(r"""() => {
                    const el = document.querySelector('#nav-cart-count, span.nav-cart-count');
                    if (!el) return null;
                    const n = parseInt(el.textContent.trim(), 10);
                    return Number.isFinite(n) ? n : null;
                }""")
                if isinstance(txt, int):
                    cart_count = txt
            except Exception:
                pass

            # Heuristic for success: ATC confirmation in the URL OR the page contains
            # an "Added to cart" indicator. If neither, still treat the cart count as truth.
            success = False
            try:
                if "huc/v1/initiate" in page.url or "/cart/add-to-cart" in page.url or "/gp/cart" in page.url:
                    success = True
                else:
                    has_added = await page.evaluate(r"""() => {
                        const t = document.body.innerText || '';
                        return /added to (your )?cart/i.test(t)
                            || !!document.querySelector('[data-feature-id="huc-atc-status"], #huc-v2-order-row-confirm-text');
                    }""")
                    if has_added:
                        success = True
            except Exception:
                pass

            if success:
                return {"success": True, "message": f"Added to cart" + (f" (cart={cart_count})" if cart_count is not None else ""), "cart_count": cart_count}

            # Fallback: button click + URL didn't move = OOS / variation / blocked
            return {"success": False, "message": "Click registered but no cart confirmation — possibly OOS, variation prompt, or CAPTCHA", "cart_count": cart_count}
        finally:
            try: await ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass


async def clear_cart(account: Account,
                     user_agent: str,
                     browser_channel: Optional[str],
                     headless: bool = True) -> dict:
    """Remove all items from this account's cart. Useful before ATC to ensure
    a clean cart state so we know exactly what we're checking out."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "message": "playwright not installed", "removed": 0}

    session_path = session_path_for(account.id)
    if not session_path.exists():
        return {"success": False, "message": "No saved session", "removed": 0}

    proxy = _proxy_to_playwright(account.proxy)
    async with async_playwright() as pw:
        launch_kw = {"headless": headless, "args": list(_BROWSER_ARGS)}
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kw)
        try:
            ctx  = await browser.new_context(
                user_agent=user_agent, viewport={"width": 1280, "height": 900},
                storage_state=str(session_path),
            )
            page = await ctx.new_page()
            await page.goto("https://www.amazon.com/gp/cart/view.html",
                            timeout=45000, wait_until="domcontentloaded")
            if "/ap/signin" in page.url:
                return {"success": False, "message": "Session expired", "removed": 0}
            await asyncio.sleep(2)
            removed = 0
            for _ in range(20):  # safety cap
                try:
                    btn = page.locator("input[value='Delete'], input[name^='submit.delete'], [data-action='delete']").first
                    await btn.wait_for(state="visible", timeout=2000)
                    await btn.click()
                    await asyncio.sleep(1.5)
                    removed += 1
                except Exception:
                    break
            await ctx.storage_state(path=str(session_path))
            return {"success": True, "message": f"Cart cleared ({removed} item{'s' if removed!=1 else ''} removed)", "removed": removed}
        finally:
            try: await ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass


async def checkout(account: Account,
                   user_agent: str,
                   browser_channel: Optional[str],
                   headless: bool = True,
                   auto_confirm: bool = False,
                   max_total: Optional[float] = None,
                   expected_asin: Optional[str] = None) -> dict:
    """Run the full checkout flow on Amazon, starting from the cart.
    Returns a result dict with order_id (when successful), total, screenshot_path.

    Safety:
      - max_total: if set, the order is REJECTED before clicking Place Your Order
        when the displayed total exceeds this. Prevents runaway charges.
      - expected_asin: if set, abort if cart contents don't include this ASIN.
      - auto_confirm: when False, navigates to the order-review page and STOPS,
        leaving the visible browser open for you to click Place Your Order
        manually. When True, clicks it automatically (only safe when max_total
        is set as a guardrail).
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "message": "playwright not installed"}

    session_path = session_path_for(account.id)
    if not session_path.exists():
        return {"success": False, "message": "No saved session — log in first"}

    if auto_confirm and max_total is None:
        return {"success": False,
                "message": "Refusing to auto-confirm without max_total guardrail. "
                           "Set a price ceiling or run with auto_confirm=False."}

    proxy = _proxy_to_playwright(account.proxy)
    # Auto-confirm runs headless; manual review needs a visible browser
    if not auto_confirm:
        headless = False

    async with async_playwright() as pw:
        launch_kw = {"headless": headless, "args": list(_BROWSER_ARGS)}
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(
            user_agent=user_agent, viewport={"width": 1280, "height": 900},
            storage_state=str(session_path),
        )
        page = await ctx.new_page()

        async def _close_with(result):
            try: await ctx.storage_state(path=str(session_path))
            except Exception: pass
            try: await ctx.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass
            return result

        try:
            # 1. Navigate to cart
            await page.goto("https://www.amazon.com/gp/cart/view.html",
                            timeout=45000, wait_until="domcontentloaded")
            if "/ap/signin" in page.url:
                return await _close_with({"success": False, "message": "Session expired — re-login this account"})
            await asyncio.sleep(2)

            # 2. Verify cart contents
            cart_info = await page.evaluate(r"""() => {
                const items = [...document.querySelectorAll('[data-name="Active Items"] [data-asin], div[data-asin]:not([data-asin=""])')];
                const seen = new Set();
                const asins = [];
                for (const el of items) {
                    const a = el.getAttribute('data-asin');
                    if (a && /^[A-Z0-9]{10}$/.test(a) && !seen.has(a)) {
                        seen.add(a); asins.push(a);
                    }
                }
                const subtotalEl = document.querySelector('#sc-subtotal-amount-buybox .a-price, #sc-subtotal-amount-activecart, [data-feature-id="sc-subtotal"] .a-price-whole');
                const subtotal = subtotalEl ? (subtotalEl.innerText || subtotalEl.textContent || '').trim() : '';
                return {asins, subtotal, isEmpty: items.length === 0};
            }""")

            if cart_info.get("isEmpty"):
                return await _close_with({"success": False, "message": "Cart is empty — ATC first"})

            if expected_asin and expected_asin not in (cart_info.get("asins") or []):
                return await _close_with({"success": False,
                    "message": f"Cart doesn't contain expected ASIN {expected_asin}. Found: {cart_info.get('asins')}"})

            # 3. Proceed to checkout
            try:
                btn = page.locator("input[name='proceedToRetailCheckout'], input[value='Proceed to checkout'], [data-feature-id='proceed-to-checkout-action']").first
                await btn.wait_for(state="visible", timeout=10000)
                await btn.click()
            except Exception as e:
                return await _close_with({"success": False, "message": f"Could not click Proceed to Checkout: {e}"})

            await asyncio.sleep(3)

            # 4. Detect OTP / MFA challenge at checkout
            url_now = page.url
            if "/ap/cvf" in url_now or "/ap/mfa" in url_now or "claim" in url_now.lower():
                return await _close_with({"success": False,
                    "message": "OTP / verification challenge at checkout — manual login required, then retry"})

            # 5. Verify we're on the order review page
            if "/gp/buy/" not in url_now and "/checkout" not in url_now.lower():
                return await _close_with({"success": False,
                    "message": f"Unexpected URL after checkout click: {url_now}"})

            # 6. Read final total
            total_str = await page.evaluate(r"""() => {
                const sels = [
                    '#subtotals-marketplace-table .grand-total-price',
                    '.grand-total-price',
                    '#orderSummaryTotal .a-color-price',
                    '[data-feature-id="order-summary"] .grand-total-price',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) return (el.innerText || '').trim();
                }
                return '';
            }""")
            total_value: Optional[float] = None
            try:
                import re as _re
                m = _re.search(r'\$?([\d,]+\.\d{2})', total_str or '')
                if m: total_value = float(m.group(1).replace(',', ''))
            except Exception:
                pass

            # 7. Enforce max_total guardrail
            if max_total is not None and total_value is not None and total_value > max_total:
                return await _close_with({
                    "success": False,
                    "total":   total_value,
                    "total_str": total_str,
                    "message": f"Order total ${total_value:.2f} exceeds max_total ${max_total:.2f} — REFUSED",
                })

            # 8. Click Place Your Order or stop and let the user do it
            if not auto_confirm:
                return await _close_with({
                    "success": True,
                    "auto_confirmed": False,
                    "total":   total_value,
                    "total_str": total_str,
                    "message": f"Ready to place order (total {total_str or '?'}). Visible browser left open — click Place Your Order yourself when ready.",
                    "browser_left_open": False,  # we still close it; future improvement: keep open
                })

            try:
                place = page.locator(
                    "input[name='placeYourOrder1'], "
                    "#placeYourOrder input[type='submit'], "
                    "input[id^='submitOrderButton'], "
                    "input[name^='placeYourOrder']"
                ).first
                await place.wait_for(state="visible", timeout=8000)
                await place.click()
            except Exception as e:
                return await _close_with({"success": False, "message": f"Could not click Place Your Order: {e}"})

            await asyncio.sleep(5)

            # 9. Capture confirmation
            order_id = await page.evaluate(r"""() => {
                const text = document.body.innerText || '';
                const m1 = text.match(/Order #\s*([\d\-]+)/i);
                if (m1) return m1[1];
                const m2 = text.match(/Order number:?\s*([\d\-]+)/i);
                if (m2) return m2[1];
                return '';
            }""")

            return await _close_with({
                "success": bool(order_id) or "thank-you" in page.url.lower() or "thankyou" in page.url.lower(),
                "auto_confirmed": True,
                "order_id": order_id or None,
                "total":   total_value,
                "total_str": total_str,
                "message": (f"Order placed (#{order_id})" if order_id else
                            "Place Your Order clicked — couldn't read order ID. Verify manually."),
            })
        except Exception as e:
            return await _close_with({"success": False, "message": f"Checkout crashed: {e}"})


async def open_browser(account: Account,
                        user_agent: str,
                        browser_channel: Optional[str]) -> dict:
    """Open a visible Chrome window using the account's saved storage_state,
    navigate to amazon.com, and LEAVE IT OPEN. Returns {pw, browser, ctx, page}."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"error": "playwright not installed"}

    session_path = session_path_for(account.id)
    if not session_path.exists():
        return {"error": "No saved session — log in first"}

    proxy = _proxy_to_playwright(account.proxy)
    try:
        pw = await async_playwright().start()
        launch_kw = {"headless": False, "args": list(_BROWSER_ARGS)}
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 900},
            storage_state=str(session_path),
        )
        page = await ctx.new_page()
        try:
            await page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception:
            pass
        return {"pw": pw, "browser": browser, "ctx": ctx, "page": page}
    except Exception as e:
        return {"error": str(e)}


async def health_check(account: Account,
                       user_agent: str,
                       browser_channel: Optional[str]) -> str:
    """Quick headless probe: load the Amazon home page through the account's
    saved session + pinned proxy and report whether it's logged in.
    Returns one of STATUS_OK / STATUS_EXPIRED / STATUS_ERROR."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return STATUS_ERROR

    session_path = session_path_for(account.id)
    if not session_path.exists():
        return STATUS_EXPIRED

    proxy = _proxy_to_playwright(account.proxy)
    async with async_playwright() as pw:
        launch_kw = {"headless": True, "args": list(_BROWSER_ARGS)}
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy
        browser = await pw.chromium.launch(**launch_kw)
        try:
            ctx  = await browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 800},
                storage_state=str(session_path),
            )
            page = await ctx.new_page()
            await page.goto(HOME_URL, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            return STATUS_OK if await _is_logged_in(page) else STATUS_EXPIRED
        except Exception:
            return STATUS_ERROR
        finally:
            try: await browser.close()
            except Exception: pass
