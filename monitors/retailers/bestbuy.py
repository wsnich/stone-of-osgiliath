"""
Best Buy automation — login, ATC, and health check.

Uses a per-account PERSISTENT Chrome profile (launch_persistent_context) with
channel="chrome" so:
  - Browser fingerprint matches a real installed Chrome (Akamai-friendly)
  - Cookies, history, prefs persist naturally — no storage_state needed
  - The profile builds up over time, making it look like a long-lived user
  - Headed by default; the user can intervene at any point

ATC behavior:
  - Visible by default (no proxy, no headless)
  - On success: window is LEFT OPEN so the user can review the cart and
    continue to checkout manually
  - On failure: window auto-closes

URLs:
  - Home:  https://www.bestbuy.com/
  - Login: https://www.bestbuy.com/identity/global/signin
  - PDP:   https://www.bestbuy.com/site/x/{SKU}.p?skuId={SKU}
  - Cart:  https://www.bestbuy.com/cart
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

log = logging.getLogger("bestbuy")

LOGIN_URL = "https://www.bestbuy.com/identity/global/signin"
HOME_URL  = "https://www.bestbuy.com/"

_BROWSER_ARGS = [
    "--password-store=basic",
    "--disable-save-password-bubble",
]


def url_from_sku(sku: str) -> str:
    return f"https://www.bestbuy.com/site/x/{sku}.p?skuId={sku}"


def _profile_dir_for(account: Account) -> Path:
    """Per-account dedicated Chrome profile directory."""
    base = session_path_for(account.id).parent  # accounts/
    p = base / f"{account.id}_chrome_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _launch_persistent(headless: bool = False, browser_channel: Optional[str] = "chrome",
                             user_data_dir: Optional[Path] = None,
                             user_agent: Optional[str] = None):
    """Open a persistent Chrome context. Returns (pw, context)."""
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright  # type: ignore
    pw = await async_playwright().start()
    launch_kw = {
        "headless": headless,
        "args": list(_BROWSER_ARGS),
        "channel": browser_channel or "chrome",  # force real Chrome
        "viewport": {"width": 1280, "height": 900},
    }
    if user_agent:
        launch_kw["user_agent"] = user_agent
    ctx = await pw.chromium.launch_persistent_context(str(user_data_dir), **launch_kw)
    return pw, ctx


async def _is_logged_in(page) -> bool:
    """Strictly POSITIVE check — requires explicit 'Hi, FirstName' greeting."""
    try:
        result = await page.evaluate(r"""() => {
            if (location.pathname.startsWith('/identity/')) return false;
            const candidates = [
                '.user-name',
                '.account-button .v-button-link',
                '[data-track="Account"]',
                '.bby-account-link',
                'header [data-cl-cmp="HelloMenu"]',
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                const t = el ? (el.textContent || '').trim() : '';
                if (t && /^Hi[,\s]/i.test(t)) return true;
            }
            const headerText = (document.querySelector('header') || document.body).innerText || '';
            return /\bHi,\s+[A-Z][a-zA-Z]+/.test(headerText.slice(0, 1500));
        }""")
        return bool(result)
    except Exception:
        return False


async def _warm_up_session(page, on_status) -> None:
    try:
        await on_status(STATUS_AWAITING, "Warming session — browsing homepage…")
        await asyncio.sleep(5)
        try: await page.evaluate("window.scrollTo({top: 600, behavior: 'smooth'})")
        except Exception: pass
        await asyncio.sleep(2)
        try: await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        except Exception: pass

        await on_status(STATUS_AWAITING, "Warming session — visiting a category…")
        try:
            await page.goto("https://www.bestbuy.com/site/electronics/computer-cards-components/abcat0507000.c",
                            timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(4)
        except Exception:
            pass

        await on_status(STATUS_AWAITING, "Returning to homepage for sign-in…")
        try:
            await page.goto(HOME_URL, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception:
            pass
    except Exception:
        pass


async def login(account: Account,
                user_agent: str,
                browser_channel: Optional[str],
                on_status: Callable[[str, str], Awaitable[None]],
                login_timeout_sec: int = 300) -> bool:
    """First-time login into the per-account persistent profile."""
    profile_dir = _profile_dir_for(account)
    new_profile = not any(profile_dir.iterdir())  # empty = brand new

    pw = None
    ctx = None
    try:
        pw, ctx = await _launch_persistent(
            headless=False,
            browser_channel=browser_channel or "chrome",
            user_data_dir=profile_dir,
            user_agent=user_agent,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await on_status(STATUS_AWAITING, f"Opening Best Buy for '{account.name}'…")
        await page.goto(HOME_URL, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        if await _is_logged_in(page):
            await on_status(STATUS_OK, "Already logged in (persistent profile)")
            return True

        if new_profile:
            await _warm_up_session(page, on_status)

        await on_status(STATUS_AWAITING,
            f"*** Click 'Sign In' in the header and log into Best Buy as '{account.email}' "
            "in the visible browser window. ***")

        for _ in range(login_timeout_sec):
            await asyncio.sleep(1)
            try:
                if await _is_logged_in(page):
                    await on_status(STATUS_OK, f"Logged in as '{account.name}'")
                    return True
            except Exception:
                pass

        await on_status(STATUS_ERROR, "Login timed out (5 minutes)")
        return False
    finally:
        try:
            if ctx: await ctx.close()
        except Exception: pass
        try:
            if pw: await pw.stop()
        except Exception: pass


async def add_to_cart(account: Account,
                      url: str,
                      user_agent: str,
                      browser_channel: Optional[str],
                      headless: bool = True,   # ignored for Best Buy — always visible
                      quantity: int = 1) -> dict:
    """Add to cart via the per-account real-Chrome profile.

    Visible by default. On success the browser window is LEFT OPEN so you can
    review the cart / continue to checkout. On failure the window auto-closes.
    """
    profile_dir = _profile_dir_for(account)
    if not any(profile_dir.iterdir()):
        return {"success": False, "message": "No profile yet — log in first", "cart_count": None}

    # Always visible; ignore headless flag for Best Buy
    pw, ctx = await _launch_persistent(
        headless=False,
        browser_channel=browser_channel or "chrome",
        user_data_dir=profile_dir,
        user_agent=user_agent,
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    async def _close_all():
        try: await ctx.close()
        except Exception: pass
        try: await pw.stop()
        except Exception: pass

    try:
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            await _close_all()
            return {"success": False, "message": f"Failed to load product: {e}", "cart_count": None}

        if "/identity/global/signin" in page.url:
            await _close_all()
            return {"success": False, "message": "Session expired — re-login this account", "cart_count": None}

        atc_selectors = [
            "button.add-to-cart-button",
            ".fulfillment-add-to-cart-button button",
            "button[data-button-state='ADD_TO_CART']",
            "button[data-track='Add to Cart']",
            "[data-feature-name='AddToCartButton'] button",
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
            await _close_all()
            return {"success": False, "message": "Could not find Add to Cart button (OOS / sold out / variation page?)", "cart_count": None}

        await asyncio.sleep(2)

        cart_count: Optional[int] = None
        try:
            txt = await page.evaluate(r"""() => {
                const el = document.querySelector('.cart-link__count, [data-track="Cart"] .v-fw-medium, .cart-count');
                if (!el) return null;
                const n = parseInt(el.textContent.trim(), 10);
                return Number.isFinite(n) ? n : null;
            }""")
            if isinstance(txt, int):
                cart_count = txt
        except Exception:
            pass

        success = False
        try:
            if "/cart" in page.url and "/cart-icon" not in page.url:
                success = True
            else:
                has_added = await page.evaluate(r"""() => {
                    const t = document.body.innerText || '';
                    return /added to (your )?cart/i.test(t)
                        || !!document.querySelector('[data-track="Cart"] .v-fw-medium');
                }""")
                if has_added:
                    success = True
        except Exception:
            pass

        if success:
            # Leave the window OPEN — user can continue to checkout manually
            return {
                "success": True,
                "message": "Added to cart" + (f" (cart={cart_count})" if cart_count is not None else "") + " — browser left open",
                "cart_count": cart_count,
                "browser_left_open": True,
            }

        # Failure — close the window
        await _close_all()
        return {"success": False,
                "message": "Click registered but no cart confirmation — possibly OOS, blocked, or CAPTCHA",
                "cart_count": cart_count}
    except Exception as e:
        await _close_all()
        return {"success": False, "message": f"Crashed: {e}", "cart_count": None}


async def open_browser(account: Account,
                        user_agent: str,
                        browser_channel: Optional[str]) -> dict:
    """Open a visible Chrome window using the account's saved profile, navigate
    to the home page, and LEAVE IT OPEN. Caller is responsible for closing later.
    Returns {pw, ctx, page} on success, or {error: str} on failure."""
    profile_dir = _profile_dir_for(account)
    if not any(profile_dir.iterdir()):
        return {"error": "No profile yet — log in first"}
    try:
        pw, ctx = await _launch_persistent(
            headless=False,
            browser_channel=browser_channel or "chrome",
            user_data_dir=profile_dir,
            user_agent=user_agent,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception:
            pass
        return {"pw": pw, "ctx": ctx, "page": page}
    except Exception as e:
        return {"error": str(e)}


async def health_check(account: Account,
                       user_agent: str,
                       browser_channel: Optional[str]) -> str:
    profile_dir = _profile_dir_for(account)
    if not any(profile_dir.iterdir()):
        return STATUS_EXPIRED

    pw = None
    ctx = None
    try:
        pw, ctx = await _launch_persistent(
            headless=True,
            browser_channel=browser_channel or "chrome",
            user_data_dir=profile_dir,
            user_agent=user_agent,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(HOME_URL, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        return STATUS_OK if await _is_logged_in(page) else STATUS_EXPIRED
    except Exception:
        return STATUS_ERROR
    finally:
        try:
            if ctx: await ctx.close()
        except Exception: pass
        try:
            if pw: await pw.stop()
        except Exception: pass
