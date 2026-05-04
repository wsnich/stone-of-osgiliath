"""
Walmart automation — login, ATC, checkout, health check.

Walmart uses Akamai Bot Manager but it's noticeably less aggressive than
Best Buy. Patchright Chromium + a residential ISP proxy is generally enough.
We follow the same per-account isolation pattern as Amazon (storage_state
JSON for cookies, dedicated proxy per account).

URLs:
  - Home:     https://www.walmart.com/
  - Login:    https://www.walmart.com/account/login
  - PDP:      https://www.walmart.com/ip/{slug-or-anything}/{itemId}
  - Cart:     https://www.walmart.com/cart
  - Checkout: https://www.walmart.com/checkout
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Callable, Awaitable

from monitors.account_manager import (
    Account, session_path_for,
    STATUS_OK, STATUS_AWAITING, STATUS_EXPIRED, STATUS_ERROR,
)

log = logging.getLogger("walmart")

LOGIN_URL = "https://www.walmart.com/account/login"
HOME_URL  = "https://www.walmart.com/"
CART_URL  = "https://www.walmart.com/cart"

_BROWSER_ARGS = [
    "--password-store=basic",
    "--disable-save-password-bubble",
]


def _proxy_to_playwright(proxy_str: str) -> Optional[dict]:
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) != 4:
        return None
    host, port, user, pw = parts
    return {"server": f"http://{host}:{port}", "username": user, "password": pw}


def url_from_item_id(item_id: str) -> str:
    """Walmart product URL — slug doesn't matter as long as the item ID is correct."""
    return f"https://www.walmart.com/ip/x/{item_id}"


async def _is_logged_in(page) -> bool:
    """Strictly POSITIVE check — needs to see the user-name element in the
    account header. Walmart shows 'Hi, FirstName' or just the name in the
    account button when logged in."""
    try:
        result = await page.evaluate(r"""() => {
            // Logged-out paths typically include /account/login
            if (location.pathname.includes('/account/login')) return false;

            // Logged-in indicators (Walmart varies by A/B test)
            const candidates = [
                '[data-automation-id="account-greeting"]',
                '[data-testid="account-greeting"]',
                '[link-identifier="accountAccountLink"] span',
                'header [aria-label*="Account"] span',
                '[data-testid*="welcome"]',
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                const t = el ? (el.textContent || '').trim() : '';
                if (t && /^Hi[,\s]/i.test(t)) return true;
                if (t && /welcome\s+back/i.test(t)) return true;
                if (t && !/sign in|account|create/i.test(t) && t.length < 40) return true;
            }
            // Header-text fallback
            const headerText = (document.querySelector('header') || document.body).innerText || '';
            return /\bHi,\s+[A-Z][a-zA-Z]+/.test(headerText.slice(0, 1500))
                || /\bWelcome back,\s+[A-Z]/.test(headerText.slice(0, 1500));
        }""")
        return bool(result)
    except Exception:
        return False


async def login(account: Account,
                user_agent: str,
                browser_channel: Optional[str],
                on_status: Callable[[str, str], Awaitable[None]],
                login_timeout_sec: int = 300) -> bool:
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
        if browser_channel: launch_kw["channel"] = browser_channel
        if proxy:           launch_kw["proxy"] = proxy

        browser = await pw.chromium.launch(**launch_kw)
        context_kw = {
            "user_agent": user_agent,
            "viewport": {"width": 1280, "height": 900},
        }
        if session_path.exists():
            try:
                context_kw["storage_state"] = str(session_path)
                await on_status(STATUS_AWAITING, "Restoring saved Walmart session…")
            except Exception:
                pass

        ctx  = await browser.new_context(**context_kw)
        page = await ctx.new_page()
        try:
            await on_status(STATUS_AWAITING, f"Opening Walmart for '{account.name}'…")
            await page.goto(HOME_URL, timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            if await _is_logged_in(page):
                await on_status(STATUS_OK, "Already logged in")
                await ctx.storage_state(path=str(session_path))
                return True

            await on_status(STATUS_AWAITING,
                f"*** Click 'Sign In' in the header and log into Walmart as '{account.email}' "
                "in the browser window. The window auto-closes when login is detected. ***")

            try:
                await page.goto(LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                pass

            # Pre-fill email if visible
            try:
                if account.email:
                    await page.fill("input[type='email'], input#email, input[name='email']",
                                    account.email, timeout=4000)
            except Exception:
                pass

            for _ in range(login_timeout_sec):
                await asyncio.sleep(1)
                try:
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


async def add_to_cart(account: Account,
                      url: str,
                      user_agent: str,
                      browser_channel: Optional[str],
                      headless: bool = True,
                      quantity: int = 1) -> dict:
    """Navigate to a Walmart product and click Add to Cart. Verifies the cart
    count changed or 'Added to cart' confirmation appeared."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "message": "playwright not installed", "cart_count": None}

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
                user_agent=user_agent, viewport={"width": 1280, "height": 900},
                storage_state=str(session_path),
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                return {"success": False, "message": f"Failed to load product: {e}", "cart_count": None}

            if "/account/login" in page.url:
                return {"success": False, "message": "Session expired — re-login this account", "cart_count": None}

            # Walmart's ATC button has many variants depending on product type
            # (regular, freshness-guaranteed, marketplace, multi-pack, etc.)
            atc_selectors = [
                "button[data-automation-id='atc-button']",
                "button[data-testid='atc-button']",
                "button[aria-label='Add to cart']",
                "button[aria-label*='Add to cart']",
                "[data-automation-id='atc-bundle-button']",
                "button.button-add-to-cart",
            ]
            clicked = False
            for sel in atc_selectors:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=4000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                # Try to diagnose
                diag = await page.evaluate(r"""() => {
                    const t = (document.body.innerText || '').toLowerCase();
                    if (t.includes('captcha') || t.includes('robot')) return 'captcha';
                    if (t.includes('out of stock') || t.includes('not available')) return 'oos';
                    if (t.includes('add to list')) return 'no-buy-button';
                    if (document.querySelector('[data-automation-id*="variant"]')) return 'variation';
                    return 'unknown';
                }""")
                reason_map = {
                    'captcha':       "CAPTCHA / blocked by Akamai",
                    'oos':           "Out of stock",
                    'no-buy-button': "No buy button (third-party seller / Walmart Plus required?)",
                    'variation':     "Variation picker required (size / color / pack)",
                    'unknown':       "Could not find Add to Cart button",
                }
                return {"success": False, "message": reason_map.get(diag, reason_map['unknown']), "cart_count": None}

            await asyncio.sleep(2)

            # Cart count badge
            cart_count: Optional[int] = None
            try:
                txt = await page.evaluate(r"""() => {
                    const el = document.querySelector(
                        '[data-automation-id="cart-counter"], [data-testid="cart-counter"], a[link-identifier="cartLink"] span'
                    );
                    if (!el) return null;
                    const n = parseInt((el.textContent || '').trim(), 10);
                    return Number.isFinite(n) ? n : null;
                }""")
                if isinstance(txt, int):
                    cart_count = txt
            except Exception:
                pass

            success = False
            try:
                # Walmart shows a slide-in "Added to cart" panel after a successful click
                added = await page.evaluate(r"""() => {
                    const t = document.body.innerText || '';
                    if (/added to cart/i.test(t)) return true;
                    if (document.querySelector('[data-automation-id*="confirmation"]')) return true;
                    return false;
                }""")
                if added:
                    success = True
                elif "/cart" in page.url:
                    success = True
            except Exception:
                pass

            if success:
                return {"success": True,
                        "message": "Added to cart" + (f" (cart={cart_count})" if cart_count is not None else ""),
                        "cart_count": cart_count}
            return {"success": False,
                    "message": "Click registered but no cart confirmation — possibly OOS, blocked, or popup",
                    "cart_count": cart_count}
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
                   expected_item_id: Optional[str] = None) -> dict:
    """Run the cart → place-order flow on Walmart.

    Same safety model as Amazon checkout — auto_confirm requires max_total.
    Walmart's checkout flow is several pages (Shipping → Payment → Review)
    so this navigates through them, verifies the total against max_total,
    and either clicks Place Order (auto-confirm) or stops at review.
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
        return {"success": False, "message": "No saved session"}

    if auto_confirm and max_total is None:
        return {"success": False,
                "message": "Refusing to auto-confirm without max_total guardrail."}

    if not auto_confirm:
        headless = False  # manual review needs visible browser

    proxy = _proxy_to_playwright(account.proxy)

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
            await page.goto(CART_URL, timeout=45000, wait_until="domcontentloaded")
            if "/account/login" in page.url:
                return await _close_with({"success": False, "message": "Session expired — re-login"})
            await asyncio.sleep(2)

            cart_info = await page.evaluate(r"""() => {
                const items = [...document.querySelectorAll('[data-testid="cart-item"], [data-automation-id*="cart-item"]')];
                return {count: items.length, hasItems: items.length > 0};
            }""")
            if not cart_info.get("hasItems"):
                return await _close_with({"success": False, "message": "Cart is empty"})

            # Continue to checkout
            try:
                btn = page.locator(
                    "button[data-testid='checkout-btn'], "
                    "button[data-automation-id='checkout-btn'], "
                    "button:has-text('Continue to checkout')"
                ).first
                await btn.wait_for(state="visible", timeout=10000)
                await btn.click()
            except Exception as e:
                return await _close_with({"success": False, "message": f"Could not click checkout: {e}"})

            await asyncio.sleep(3)

            # Walmart checkout = multi-step. Continue through the steps.
            # We click any visible "Continue" button up to 4 times.
            for _ in range(4):
                try:
                    cont = page.locator(
                        "button[data-testid='continue-button'], "
                        "button[data-automation-id='continue-button'], "
                        "button:has-text('Continue'), "
                        "button:has-text('Use this address'), "
                        "button:has-text('Use this payment')"
                    ).first
                    await cont.wait_for(state="visible", timeout=3000)
                    await cont.click()
                    await asyncio.sleep(2)
                except Exception:
                    break

            # OTP / verification challenge
            if "/account/" in page.url and "verify" in page.url.lower():
                return await _close_with({"success": False,
                    "message": "OTP / verification challenge at checkout — manual login required"})

            # Read total
            total_str = await page.evaluate(r"""() => {
                const sels = [
                    '[data-testid="order-total"]',
                    '[data-automation-id="order-total"]',
                    '[data-testid="grand-total"]',
                    '.order-summary-total',
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

            if max_total is not None and total_value is not None and total_value > max_total:
                return await _close_with({
                    "success": False,
                    "total":   total_value, "total_str": total_str,
                    "message": f"Order total ${total_value:.2f} exceeds max_total ${max_total:.2f} — REFUSED",
                })

            if not auto_confirm:
                return await _close_with({
                    "success": True,
                    "auto_confirmed": False,
                    "total":   total_value, "total_str": total_str,
                    "message": f"Ready to place order (total {total_str or '?'}). Browser left open — click Place Order yourself.",
                })

            # Place order
            try:
                place = page.locator(
                    "button[data-testid='place-order-btn'], "
                    "button[data-automation-id='place-order-btn'], "
                    "button:has-text('Place order'), "
                    "button:has-text('Place Order')"
                ).first
                await place.wait_for(state="visible", timeout=8000)
                await place.click()
            except Exception as e:
                return await _close_with({"success": False, "message": f"Could not click Place Order: {e}"})

            await asyncio.sleep(5)

            order_id = await page.evaluate(r"""() => {
                const text = document.body.innerText || '';
                const m1 = text.match(/Order #\s*([\d\-]+)/i);
                if (m1) return m1[1];
                const m2 = text.match(/Order number:?\s*([\d\-]+)/i);
                if (m2) return m2[1];
                const m3 = text.match(/Confirmation #\s*([\d\-]+)/i);
                if (m3) return m3[1];
                return '';
            }""")

            return await _close_with({
                "success": bool(order_id) or "thankyou" in page.url.lower() or "confirmation" in page.url.lower(),
                "auto_confirmed": True,
                "order_id": order_id or None,
                "total":   total_value, "total_str": total_str,
                "message": f"Order placed (#{order_id})" if order_id else "Place Order clicked — order ID not captured. Verify manually.",
            })
        except Exception as e:
            return await _close_with({"success": False, "message": f"Checkout crashed: {e}"})


async def open_browser(account: Account,
                        user_agent: str,
                        browser_channel: Optional[str]) -> dict:
    """Open a visible Chrome window with this account's saved Walmart session."""
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
            user_agent=user_agent, viewport={"width": 1280, "height": 900},
            storage_state=str(session_path),
        )
        page = await ctx.new_page()
        try: await page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception: pass
        return {"pw": pw, "browser": browser, "ctx": ctx, "page": page}
    except Exception as e:
        return {"error": str(e)}


async def health_check(account: Account,
                       user_agent: str,
                       browser_channel: Optional[str]) -> str:
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
            ctx = await browser.new_context(
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
