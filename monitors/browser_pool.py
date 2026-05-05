"""
Persistent off-screen browser pool for checkout accounts.

Each enabled Amazon/Walmart account gets one always-on browser instance,
launched at app startup with --window-position=-9000,-9000 so it lives
off-screen. ATC reuses these browsers (open new tab in existing context),
giving us ~3s ATC instead of ~10s. When user attention is needed (after
successful ATC, OTP prompt, etc.) we move the window on-screen via CDP.

Key methods:
    start_for(account, ...)       — launch and store
    stop_for(account_id)          — clean shutdown
    get_page(account_id)          — fresh blank page in the account's context
    show_window(account_id)       — move on-screen
    hide_window(account_id)       — move off-screen
    is_active(account_id)         — health check

Per-account asyncio.Lock prevents two ATC operations from colliding in the
same context.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from monitors.account_manager import Account, session_path_for

log = logging.getLogger("browser_pool")

# Pool browsers launch at the default Chrome position (visible from the start).
# The previous off-screen-then-surface design proved unreliable on Windows 11:
# CDP setWindowBounds didn't always change Z-order, the topmost-then-not trick
# was finicky, and dead pages required Win32 fallbacks. Simpler to just keep
# them visible — user can tile/minimize manually if they want them out of the way.
_WINDOW_WIDTH, _WINDOW_HEIGHT = 1280, 900

# Init script injected into every page in the pool. Replaces the WebAuthn
# API entry points with rejecting stubs so Amazon's sign-in cannot trigger
# Windows Hello no matter what Chromium feature flags are in play.
_DISABLE_WEBAUTHN_JS = r"""
(() => {
  const reject = () => Promise.reject(new DOMException("not allowed", "NotAllowedError"));
  try {
    if (navigator.credentials) {
      navigator.credentials.get = reject;
      navigator.credentials.create = reject;
      navigator.credentials.preventSilentAccess = reject;
    }
  } catch (e) { /* ignore */ }
  try {
    if (window.PublicKeyCredential) {
      window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = () => Promise.resolve(false);
      window.PublicKeyCredential.isConditionalMediationAvailable = () => Promise.resolve(false);
    }
  } catch (e) { /* ignore */ }
})();
"""

_BROWSER_ARGS_BASE = [
    "--password-store=basic",
    "--disable-save-password-bubble",
    # Suppress Windows Hello PIN prompts during retailer sign-in. Targeted set
    # — broader --disable-features values are detectable by anti-bot fingerprinters.
    "--disable-features=WebAuthenticationConditionalUI,PasswordManagerOnboarding,PasswordCheck",
    f"--window-size={_WINDOW_WIDTH},{_WINDOW_HEIGHT}",
]


class PersistentBrowser:
    """Single account's persistent browser handle."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.pw = None
        self.browser = None
        self.ctx = None
        self.lock = asyncio.Lock()      # serialize ATC ops on this context
        self.visible = False             # current on/off-screen state
        self.session_path = None        # Path to storage_state JSON for this account
        # Pre-warmed product tabs keyed by identifier (ASIN/SKU/ItemID).
        # When a deal hits, we can grab an already-loaded page instead of
        # paying the navigation cost again.
        self.warmed_pages: dict[str, "Page"] = {}
        # Stored URL per identifier so the periodic refresher knows where
        # to re-navigate when Amazon idle-kills a tab. Survives even if
        # the Page object dies.
        self.warmed_urls: dict[str, str] = {}

    async def start(self, account: Account, user_agent: str,
                    browser_channel: Optional[str]) -> bool:
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                log.warning("playwright not installed")
                return False

        session_path = session_path_for(account.id)
        self.session_path = session_path
        if not session_path.exists():
            log.info(f"[{account.name}] no saved session — skipping persistent launch")
            return False

        # Account's pinned proxy
        proxy = None
        if account.proxy:
            parts = account.proxy.split(":")
            if len(parts) == 4:
                host, port, user, pw_ = parts
                proxy = {"server": f"http://{host}:{port}", "username": user, "password": pw_}

        try:
            self.pw = await async_playwright().start()
            launch_kw = {
                "headless": False,            # visible BUT off-screen
                "args": list(_BROWSER_ARGS_BASE),
            }
            # Pool only serves Amazon/Walmart. Both trigger Windows Hello
            # PIN prompts when launched via real Chrome (browser_channel
            # ="chrome") because real Chrome integrates with the Windows
            # Credential Manager. Force bundled Chromium for the pool
            # regardless of the global config — Best Buy uses its own
            # persistent_context launch path with channel="chrome".
            _ = browser_channel
            if proxy:
                launch_kw["proxy"] = proxy
            self.browser = await self.pw.chromium.launch(**launch_kw)
            self.ctx = await self.browser.new_context(
                user_agent=user_agent,
                viewport={"width": _WINDOW_WIDTH, "height": _WINDOW_HEIGHT},
                storage_state=str(session_path),
            )
            # Kill the WebAuthn API at the page level. Without this, Amazon's
            # sign-in form calls navigator.credentials.get() and triggers
            # Windows Hello regardless of which Chromium feature flags are set.
            # Stubbing the API to a permanent rejection makes the page fall
            # back to email + password.
            try:
                await self.ctx.add_init_script(_DISABLE_WEBAUTHN_JS)
            except Exception:
                pass
            log.info(f"[{account.name}] persistent browser started off-screen")
            return True
        except Exception as e:
            log.warning(f"[{account.name}] persistent launch failed: {e}")
            await self.stop()
            return False

    async def get_page(self):
        """Return a new blank page inside the persistent context."""
        if not self.ctx:
            return None
        try:
            return await self.ctx.new_page()
        except Exception:
            return None

    async def pre_warm(self, identifier: str, url: str) -> bool:
        """Open a tab to a product URL and keep it loaded. When a deal hits
        for this identifier, the ATC dispatcher can grab the existing tab
        and click the already-rendered ATC button — saving 1-3 seconds.

        Identifier should be the retailer-specific ID (ASIN/SKU/Item#)."""
        if not self.ctx:
            return False
        self.warmed_urls[identifier] = url
        # Already warmed? Reuse.
        existing = self.warmed_pages.get(identifier)
        if existing is not None:
            try:
                _ = existing.url
                return True
            except Exception:
                self.warmed_pages.pop(identifier, None)
        try:
            page = await self.ctx.new_page()
            # commit returns as soon as response is received — page keeps
            # loading in background while we move on.
            try:
                await page.goto(url, timeout=20000, wait_until="commit")
            except Exception:
                pass
            self.warmed_pages[identifier] = page
            return True
        except Exception:
            return False

    async def refresh_warmed_tabs(self) -> int:
        """Reload (or re-open) every warmed tab so Amazon's idle timeout
        doesn't reap them. Returns count of tabs successfully refreshed.

        Acquires the pool lock so this won't race an in-flight ATC.
        """
        if not self.ctx or not self.warmed_urls:
            return 0
        async with self.lock:
            refreshed = 0
            for ident, url in list(self.warmed_urls.items()):
                page = self.warmed_pages.get(ident)
                # Page dead or never opened — open a fresh tab
                page_alive = False
                if page is not None:
                    try:
                        _ = page.url
                        page_alive = True
                    except Exception:
                        page_alive = False
                if not page_alive:
                    try: await page.close()  # best-effort
                    except Exception: pass
                    self.warmed_pages.pop(ident, None)
                    try:
                        page = await self.ctx.new_page()
                        await page.goto(url, timeout=20000, wait_until="commit")
                        self.warmed_pages[ident] = page
                        refreshed += 1
                        continue
                    except Exception:
                        continue
                # Page alive — re-navigate to refresh the session
                try:
                    await page.goto(url, timeout=20000, wait_until="commit")
                    refreshed += 1
                except Exception:
                    # Re-navigation failed; drop and replace
                    try: await page.close()
                    except Exception: pass
                    self.warmed_pages.pop(ident, None)
                    try:
                        new_page = await self.ctx.new_page()
                        await new_page.goto(url, timeout=20000, wait_until="commit")
                        self.warmed_pages[ident] = new_page
                        refreshed += 1
                    except Exception:
                        pass
            return refreshed

    def get_warmed_page(self, identifier: str):
        """Pop and return a pre-warmed page for this identifier, or None."""
        page = self.warmed_pages.pop(identifier, None)
        if page is None:
            return None
        try:
            _ = page.url   # raises if page closed
            return page
        except Exception:
            return None

    async def save_session(self) -> bool:
        """Write the live context's storage_state (cookies, localStorage)
        back to the session JSON on disk. Without this, manual logins inside
        the pool browser are lost on app restart — the next launch reloads
        the stale cookies that were on disk before the user signed in."""
        if not self.ctx or not self.session_path:
            return False
        try:
            await self.ctx.storage_state(path=str(self.session_path))
            log.info(f"[{self.account_id}] session saved ({self.session_path.name})")
            return True
        except Exception as e:
            log.warning(f"[{self.account_id}] save_session failed: {e}")
            return False

    async def show_window(self) -> bool:
        """Pool browsers are now always visible — no off-screen mode. Kept
        as a no-op so existing callers don't break."""
        self.visible = True
        return True

    async def hide_window(self) -> bool:
        """No-op: pool browsers stay visible. Kept for API compat."""
        self.visible = True
        return False

    def is_alive(self) -> bool:
        if not self.ctx:
            return False
        try:
            _ = self.ctx.pages   # raises if context closed
            return True
        except Exception:
            return False

    async def stop(self) -> None:
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        self.ctx = None
        self.browser = None
        self.pw = None


class BrowserPool:
    """Singleton pool of PersistentBrowser instances keyed by account id."""

    def __init__(self):
        self._handles: dict[str, PersistentBrowser] = {}

    async def start_for(self, account: Account, user_agent: str,
                        browser_channel: Optional[str]) -> bool:
        # Persistent off-screen browsers only make sense for retailers we drive
        # programmatically with `keep_open`-style flows. Best Buy uses a
        # different model (launch_persistent_context) so it stays out of the pool.
        if account.retailer not in ("amazon", "walmart"):
            return False
        if account.id in self._handles and self._handles[account.id].is_alive():
            return True
        handle = PersistentBrowser(account.id)
        ok = await handle.start(account, user_agent, browser_channel)
        if ok:
            self._handles[account.id] = handle
        return ok

    def get(self, account_id: str) -> Optional[PersistentBrowser]:
        h = self._handles.get(account_id)
        if h and h.is_alive():
            return h
        return None

    async def stop_for(self, account_id: str) -> None:
        h = self._handles.pop(account_id, None)
        if h:
            await h.stop()

    async def stop_all(self) -> None:
        for aid in list(self._handles.keys()):
            await self.stop_for(aid)

    def all_active_ids(self) -> list[str]:
        return [aid for aid, h in self._handles.items() if h.is_alive()]

    async def refresh_all_warmed_tabs(self) -> dict[str, int]:
        """Reload warmed tabs across every active handle. Returns
        {account_id: refreshed_count} for logging."""
        results: dict[str, int] = {}
        for aid, h in list(self._handles.items()):
            if not h.is_alive():
                continue
            try:
                results[aid] = await h.refresh_warmed_tabs()
            except Exception as e:
                log.debug(f"[{aid}] refresh_warmed_tabs failed: {e}")
                results[aid] = 0
        return results

    async def save_all_sessions(self) -> int:
        """Write storage_state to disk for every active handle. Returns count
        of handles that successfully saved."""
        n = 0
        for aid, h in list(self._handles.items()):
            if not h.is_alive():
                continue
            if await h.save_session():
                n += 1
        return n


# Module-level singleton — same pattern as account_manager
browser_pool = BrowserPool()
