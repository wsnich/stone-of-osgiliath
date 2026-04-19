"""
Walmart product price & availability monitor.

Anti-detection layers (outermost → innermost):
  0. Walmart Affiliate API — if consumer_id + private_key are configured, queries
     the official walmart.io item API. Zero bot-wall risk; requires free sign-up at
     developer.walmart.com (Marketplace → API Keys).
  1. patchright instead of playwright — fixes CDP leakage that Akamai/PerimeterX
     detect at the protocol level, even when using a real Chrome binary.
  2. Real Chrome (channel="chrome") — correct TLS/JA3 fingerprint.
  3. HTTP-first checks via curl-cffi — after the browser establishes a session once,
     routine price checks use curl-cffi with Chrome TLS impersonation + saved cookies.
     curl-cffi links against BoringSSL and produces an identical JA3/JA4 fingerprint
     to Chrome, so Akamai cannot distinguish it from a real browser at the TLS layer.
  4. Localization cookies — injects locDataV3 / locGuestData / ACID so Walmart
     returns store-level stock instead of flagging the session as anomalous.
  5. Search-first navigation — arrives at the product via a search-results page
     so referrer and history look organic.
  6. Persistent session saved to disk — same identity across restarts.
  7. Comprehensive stealth JS — patches webdriver flag, plugins, permissions, etc.
  8. Human-like timing — variable delays, scrolls, mouse moves.
  9. Rotating proxies — optional list of residential proxies cycled per request.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import aiohttp

log = logging.getLogger("walmart")

SESSION_FILE = Path(__file__).parent.parent / "walmart_session.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# Matches the UA above — Chrome 124 on Windows
_SEC_CH_UA          = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
_SEC_CH_UA_MOBILE   = "?0"
_SEC_CH_UA_PLATFORM = '"Windows"'

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
]

STEALTH_SCRIPT = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});
if(!window.chrome||!window.chrome.runtime){window.chrome={app:{isInstalled:false,getDetails:function(){return null;},getIsInstalled:function(){return false;},installState:function(cb){cb('not_installed');},runningState:function(){return 'cannot_run';}},runtime:{OnInstalledReason:{CHROME_UPDATE:'chrome_update',INSTALL:'install',SHARED_MODULE_UPDATE:'shared_module_update',UPDATE:'update'},OnRestartRequiredReason:{APP_UPDATE:'app_update',OS_UPDATE:'os_update',PERIODIC:'periodic'},PlatformArch:{ARM:'arm',ARM64:'arm64',MIPS:'mips',MIPS64:'mips64',X86_32:'x86-32',X86_64:'x86-64'},PlatformOs:{ANDROID:'android',CROS:'cros',LINUX:'linux',MAC:'mac',OPENBSD:'openbsd',WIN:'win'},RequestUpdateCheckStatus:{NO_UPDATE:'no_update',THROTTLED:'throttled',UPDATE_AVAILABLE:'update_available'},connect:function(){},sendMessage:function(){}},csi:function(){},loadTimes:function(){}};}
(function(){const pd=[{name:'PDF Viewer',filename:'internal-pdf-viewer',description:'Portable Document Format'},{name:'Chrome PDF Viewer',filename:'internal-pdf-viewer',description:''},{name:'Chromium PDF Viewer',filename:'internal-pdf-viewer',description:''},{name:'Microsoft Edge PDF Viewer',filename:'internal-pdf-viewer',description:''},{name:'WebKit built-in PDF',filename:'internal-pdf-viewer',description:''}];const fa={length:pd.length};pd.forEach((p,i)=>{fa[i]=p;});fa.item=i=>fa[i]||null;fa.namedItem=n=>pd.find(p=>p.name===n)||null;fa.refresh=()=>{};Object.defineProperty(navigator,'plugins',{get:()=>fa,configurable:true});})();
(function(){const m={length:2};m[0]={type:'application/pdf',suffixes:'pdf',description:'',enabledPlugin:navigator.plugins[0]};m[1]={type:'text/pdf',suffixes:'pdf',description:'',enabledPlugin:navigator.plugins[0]};m.item=i=>m[i]||null;m.namedItem=t=>(t==='application/pdf'?m[0]:t==='text/pdf'?m[1]:null);Object.defineProperty(navigator,'mimeTypes',{get:()=>m,configurable:true});})();
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en'],configurable:true});
try{const oq=window.navigator.permissions.query.bind(window.navigator.permissions);window.navigator.permissions.query=p=>{if(p&&p.name==='notifications')return Promise.resolve({state:Notification.permission,onchange:null});return oq(p);};}catch(e){}
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8,configurable:true});
try{Object.defineProperty(navigator,'deviceMemory',{get:()=>8,configurable:true});}catch(e){}
try{Object.defineProperty(navigator,'connection',{get:()=>({downlink:10,downlinkMax:Infinity,effectiveType:'4g',onchange:null,rtt:50,saveData:false,type:'wifi'}),configurable:true});}catch(e){}
try{Object.defineProperty(window,'outerWidth',{get:()=>window.innerWidth,configurable:true});Object.defineProperty(window,'outerHeight',{get:()=>window.innerHeight+88,configurable:true});}catch(e){}
try{Object.defineProperty(screen,'colorDepth',{get:()=>24,configurable:true});Object.defineProperty(screen,'pixelDepth',{get:()=>24,configurable:true});}catch(e){}
try{if(Notification.permission==='denied')Object.defineProperty(Notification,'permission',{get:()=>'default'});}catch(e){}
try{delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;}catch(e){}
"""


@dataclass
class ProductResult:
    name: str
    url: str
    price: Optional[float]
    available: bool
    raw_price_text: str = ""
    blocked: bool = False
    error: Optional[str] = None
    image_url: Optional[str] = None
    # TCGPlayer reference fields (unused for Walmart)
    low_price: Optional[float] = None
    tcg_quantity: Optional[int] = None
    # Proxy tracking
    proxy_id: Optional[str] = None      # short ID (last 4 digits of session)
    proxy_ok: Optional[bool] = None     # True = succeeded, False = failed, None = no proxy
    proxy_type: Optional[str] = None   # "ISP", "Resi", or None (direct)
    response_bytes: int = 0            # bytes received (for bandwidth tracking)
    listing_prices: Optional[list] = None  # individual listing prices for histogram
    tcg_sales: Optional[list] = None       # recent TCGPlayer sales [{price, date, condition}]
    tcg_price_history: Optional[list] = None  # daily buckets [{date, marketPrice, low, high, sold, transactions}]

    @property
    def found_deal(self) -> bool:
        return self.available and self.price is not None and not self.blocked

    def under_limit(self, max_price: float) -> bool:
        return self.found_deal and self.price <= max_price

    @staticmethod
    def short_proxy_id(proxy_url: Optional[str]) -> Optional[str]:
        """Extract a short identifier from a proxy URL (last 4 digits of session ID in password)."""
        if not proxy_url:
            return None
        # Match session digits in the password portion (before @): e.g. bGz4eQWK-334810727
        m = re.search(r'-(\d{4,})@', proxy_url)
        if m:
            return m.group(1)[-4:]
        # Fallback: any long digit sequence in the URL
        m = re.findall(r'\d{4,}', proxy_url)
        if m:
            return m[-1][-4:]
        return None


class WalmartMonitor:
    def __init__(self):
        self._pw        = None
        self._browser   = None
        self._context   = None
        self._ua        = random.choice(USER_AGENTS)
        self._viewport  = random.choice(VIEWPORTS)
        self._warmed_up = False
        self._proxy_idx     = 0
        self._isp_proxy_idx = 0
        self._browser_cooldown_until: float = 0.0
        self._http_proxies: list[str] = []
        self._http_proxies_loaded = False
        self._warmup_lock: Optional[asyncio.Lock] = None
        self._last_response_bytes: int = 0

    # ------------------------------------------------------------------
    # HTTP proxy loader (shared proxies_file, used for curl-cffi only)
    # ------------------------------------------------------------------

    def _load_http_proxies(self, stealth_cfg: dict) -> None:
        if self._http_proxies_loaded:
            return
        self._http_proxies_loaded = True
        filename = stealth_cfg.get("proxies_file", "")
        if not filename:
            return
        filepath = Path(__file__).parent.parent / filename
        if not filepath.exists():
            return
        for raw in filepath.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                self._http_proxies.append(line)
            else:
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pw = parts
                    self._http_proxies.append(f"http://{user}:{pw}@{host}:{port}")
        if self._http_proxies:
            log.info(f"Loaded {len(self._http_proxies)} HTTP proxies for Walmart")

    def _next_http_proxy(self, stealth_cfg: dict) -> Optional[str]:
        """Get next proxy URL for curl-cffi requests (not for browser)."""
        self._load_http_proxies(stealth_cfg)
        if not self._http_proxies:
            return None
        url = self._http_proxies[self._proxy_idx % len(self._http_proxies)]
        self._proxy_idx += 1
        return url

    @staticmethod
    def _next_isp_proxy(stealth_cfg: dict) -> Optional[str]:
        """
        Get a STICKY ISP proxy — always returns the first one.
        ISP proxies are static (same IP per port), and _abck cookies
        are IP-bound, so we must use the same proxy for browser warmup
        and subsequent HTTP checks.
        """
        isp_list = stealth_cfg.get("isp_proxies") or []
        if not isp_list:
            return None
        url = isp_list[0]  # sticky — same proxy for browser + HTTP
        if not url.startswith("http"):
            parts = url.split(":")
            if len(parts) == 4:
                host, port, user, pw = parts
                url = f"http://{user}:{pw}@{host}:{port}"
        return url

    # ------------------------------------------------------------------
    # Path 0: Official Walmart.io Affiliate API (zero bot-wall risk)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_item_id(url: str) -> Optional[str]:
        """Pull the numeric Walmart item ID from a product URL."""
        # Handles: /ip/Some-Name/12345678  and  /ip/12345678
        m = re.search(r'/ip/(?:[^/]+/)?(\d{6,})', url)
        return m.group(1) if m else None

    @staticmethod
    def _affiliate_auth_headers(consumer_id: str, private_key_b64: str) -> dict:
        """
        Generate the four signed headers required by the Walmart.io item API.
        The private key must be a Base64-encoded RSA or HMAC secret as issued
        by the Walmart Developer Portal (Marketplace → API Keys).
        """
        ts  = str(int(time.time() * 1000))
        msg = f"{consumer_id}\n{ts}\n1".encode()
        try:
            key = base64.b64decode(private_key_b64)
            sig = base64.b64encode(
                hmac.new(key, msg, hashlib.sha256).digest()
            ).decode()
        except Exception as e:
            log.warning(f"Affiliate API signature failed: {e}")
            return {}
        return {
            "WM_CONSUMER.ID":        consumer_id,
            "WM_SEC.KEY_VERSION":    "1",
            "WM_CONSUMER.INTIMESTAMP": ts,
            "WM_SEC.AUTH_SIGNATURE": sig,
            "Accept":                "application/json",
        }

    async def _check_via_affiliate_api(
        self, url: str, name: str, api_cfg: dict
    ) -> Optional[ProductResult]:
        """
        Query the official Walmart.io v2 items endpoint.
        Returns a ProductResult on success, None if unconfigured or failed.

        Sign up: https://developer.walmart.com  (free, approved quickly)
        Config keys: walmart_api.consumer_id, walmart_api.private_key
        """
        consumer_id = api_cfg.get("consumer_id", "").strip()
        private_key = api_cfg.get("private_key", "").strip()
        if not consumer_id or not private_key:
            return None

        item_id = self._extract_item_id(url)
        if not item_id:
            log.debug(f"Could not extract item ID from URL: {url}")
            return None

        endpoint = f"https://developer.api.walmart.com/api-proxy/service/affil/product/v2/items?ids={item_id}&format=json"
        headers  = self._affiliate_auth_headers(consumer_id, private_key)
        if not headers:
            return None

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning(f"Affiliate API returned {resp.status} for {name}")
                        return None
                    data  = await resp.json()
                    items = data.get("items", [])
                    if not items:
                        log.warning(f"Affiliate API: no items in response for {name}")
                        return None
                    item      = items[0]
                    price     = float(item.get("salePrice") or item.get("msrp") or 0) or None
                    available = item.get("availableOnline", False)
                    stock     = item.get("stock", "")
                    if stock and stock.lower() in ("not available", "out of stock"):
                        available = False
                    log.info(f"{name}: ${price:.2f} via Affiliate API | in stock: {available}")
                    return ProductResult(
                        name=name, url=url,
                        price=price, available=available,
                        raw_price_text=f"${price:.2f}" if price else "",
                    )
        except Exception as e:
            log.warning(f"Affiliate API error for {name}: {e}")
            return None

    # ------------------------------------------------------------------
    # Path 1: curl-cffi HTTP check with Chrome TLS impersonation
    # ------------------------------------------------------------------

    @staticmethod
    def _make_loc_cookie(zip_code: str) -> str:
        """
        Build the locDataV3 cookie Walmart expects for store-level inventory.
        Without it the session looks anomalous and returns national-only data.
        """
        payload = json.dumps({
            "isZipLocated": True,
            "postalCode":   zip_code,
            "city":         "",
            "state":        "",
            "country":      "US",
            "stores":       [],
            "latLong":      "",
            "lastUpdatedTs": int(time.time() * 1000),
        }, separators=(",", ":"))
        return base64.b64encode(payload.encode()).decode()

    async def _check_via_http(self, url: str, zip_code: str = "10001", proxy: Optional[str] = None, skip_cookies: bool = False) -> Optional[str]:
        """
        Fetch the product page using curl-cffi + saved session cookies.

        curl-cffi links against BoringSSL and impersonates Chrome's TLS handshake
        (JA3/JA4 fingerprint), so Akamai cannot distinguish it from a real browser
        at the network layer.  Localization cookies (locDataV3, ACID) prevent the
        session from being flagged as an anomalous, unlocated bot session.

        Returns HTML on success, None if cookies are missing or the request fails.
        """
        # Load session cookies if available — _abck cookie is required for
        # PerimeterX to let requests through even on clean ISP proxy IPs.
        cookies = {}
        if SESSION_FILE.exists():
            try:
                session_data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                cookies = {
                    c["name"]: c["value"]
                    for c in session_data.get("cookies", [])
                    if "walmart" in c.get("domain", "")
                }
            except Exception:
                pass

        # Inject localization cookies — even without session cookies, these
        # help Walmart return store-level data instead of flagging the session.
        loc_val = self._make_loc_cookie(zip_code)
        cookies.setdefault("locDataV3",   loc_val)
        cookies.setdefault("locGuestData", loc_val)
        # ACID is Walmart's anonymous session ID — keep whatever the browser set;
        # only generate a placeholder if it's totally absent
        cookies.setdefault("ACID", base64.b64encode(
            random.randbytes(16)
        ).decode().rstrip("="))

        headers = {
            "User-Agent":                self._ua,
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept-Encoding":           "gzip, deflate, br",
            "DNT":                       "1",
            "Connection":                "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua":                 _SEC_CH_UA,
            "Sec-Ch-Ua-Mobile":          _SEC_CH_UA_MOBILE,
            "Sec-Ch-Ua-Platform":        _SEC_CH_UA_PLATFORM,
            "Sec-Fetch-Dest":            "document",
            "Sec-Fetch-Mode":            "navigate",
            "Sec-Fetch-Site":            "none" if skip_cookies else "same-origin",
            "Sec-Fetch-User":            "?1",
        }
        # Fresh visit (ISP proxy, no cookies) — don't claim to be from walmart.com
        if not skip_cookies:
            headers["Referer"] = "https://www.walmart.com/"

        try:
            from curl_cffi.requests import AsyncSession as CurlSession
            kwargs = dict(
                headers=headers,
                cookies=cookies,
                timeout=20,
                allow_redirects=True,
            )
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            async with CurlSession(impersonate="chrome124") as session:
                resp = await session.get(url, **kwargs)
                self._last_response_bytes = len(resp.content) if resp.content else 0
                if resp.status_code == 200:
                    via = "proxy" if proxy else "direct"
                    log.debug(f"HTTP check succeeded (curl-cffi / chrome124 TLS / {via})")
                    return resp.text
                log.debug(f"HTTP check returned status {resp.status_code}")
        except ImportError:
            log.debug("curl-cffi not available, falling back to aiohttp")
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(cookies=cookies, timeout=timeout) as session:
                    async with session.get(url, headers=headers, allow_redirects=True) as resp:
                        if resp.status == 200:
                            log.debug("HTTP check succeeded (aiohttp fallback)")
                            return await resp.text(errors="replace")
                        log.debug(f"HTTP check returned status {resp.status}")
            except Exception as e:
                log.debug(f"aiohttp fallback error: {e}")
        except Exception as e:
            log.debug(f"HTTP check error: {e}")

        return None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _next_proxy(self, proxies: list) -> Optional[dict]:
        """Round-robin through the proxy list."""
        if not proxies:
            return None
        proxy_url = proxies[self._proxy_idx % len(proxies)]
        self._proxy_idx += 1
        return {"server": proxy_url}

    async def _ensure_browser(self, stealth_cfg: dict) -> None:
        if self._browser and self._browser.is_connected():
            return

        try:
            from patchright.async_api import async_playwright as _apw
            log.info("Using patchright (CDP-stealth mode)")
        except ImportError:
            from playwright.async_api import async_playwright as _apw
            log.info("patchright not available — falling back to playwright")

        headless  = stealth_cfg.get("headless", True)

        self._pw = await _apw().start()

        # ISP proxies support HTTP/2 tunneling — use them for browser too
        isp_proxy = self._next_isp_proxy(stealth_cfg)
        if isp_proxy:
            parsed = re.match(r'https?://([^:]+):([^@]+)@([^:]+):(\d+)', isp_proxy)
            if parsed:
                browser_proxy = {"server": f"http://{parsed.group(3)}:{parsed.group(4)}",
                                 "username": parsed.group(1), "password": parsed.group(2)}
                log.info(f"Browser using ISP proxy: {parsed.group(3)}")
            else:
                browser_proxy = None
        else:
            browser_proxy = None

        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(
                    headless=headless,
                    args=self._build_args(headless),
                )
                if channel:
                    kwargs["channel"] = channel
                if browser_proxy:
                    kwargs["proxy"] = browser_proxy

                self._browser = await self._pw.chromium.launch(**kwargs)
                label = f"real {channel}" if channel else "Playwright Chromium"
                log.info(f"Browser: {label}, headless={headless}")
                break
            except Exception as e:
                log.debug(f"Could not launch {channel or 'Chromium'}: {e}")
        else:
            raise RuntimeError("Could not launch any browser")

        ctx_args = dict(
            user_agent=self._ua,
            viewport=self._viewport,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language":           "en-US,en;q=0.9",
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "DNT":                       "1",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Ch-Ua":                 _SEC_CH_UA,
                "Sec-Ch-Ua-Mobile":          _SEC_CH_UA_MOBILE,
                "Sec-Ch-Ua-Platform":        _SEC_CH_UA_PLATFORM,
            },
        )
        if SESSION_FILE.exists():
            ctx_args["storage_state"] = str(SESSION_FILE)
            log.info("Loaded saved session")

        self._context = await self._browser.new_context(**ctx_args)
        await self._context.add_init_script(STEALTH_SCRIPT)

    @staticmethod
    def _build_args(headless: bool) -> list[str]:
        return [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1280,800",
        ]

    async def _warmup(self) -> None:
        if self._warmed_up:
            return
        log.info("Warming up session on walmart.com…")
        page = await self._context.new_page()
        try:
            await page.goto("https://www.walmart.com", wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            page_html = await page.content()
            if self._is_technical_error(page_html):
                log.warning("Walmart technical error page during warmup — IP may be flagged. Backing off 15 min.")
                self._browser_cooldown_until = time.time() + 900
                return  # leave _warmed_up = False
            if await self._has_press_hold(page):
                log.info("Challenge on homepage during warmup — attempting to solve…")
                solved = await self._solve_press_and_hold(page)
                if not solved:
                    log.warning("Could not solve homepage challenge during warmup")
                    return   # leave _warmed_up = False, will retry next cycle
                await page.wait_for_timeout(random.randint(2000, 3000))

            for _ in range(random.randint(2, 4)):
                await page.evaluate(f"window.scrollBy({{top:{random.randint(150,400)},behavior:'smooth'}})")
                await page.wait_for_timeout(random.randint(700, 1500))
            vp = self._viewport
            await page.mouse.move(
                random.randint(100, vp["width"] - 100),
                random.randint(100, vp["height"] - 100),
            )
            await page.wait_for_timeout(random.randint(800, 1500))
            self._warmed_up = True
            await self._save_session()
        except Exception as e:
            log.warning(f"Warmup failed (non-fatal): {e}")
        finally:
            await page.close()

    async def _save_session(self) -> None:
        if self._context:
            try:
                state = await self._context.storage_state()
                # Stamp the save time so we can track session age
                state["_saved_at"] = time.time()
                SESSION_FILE.write_text(json.dumps(state), encoding="utf-8")
            except Exception as e:
                log.warning(f"Could not save session: {e}")

    def _session_age_hours(self) -> float:
        """Return how many hours ago the session was last saved, or infinity."""
        if not SESSION_FILE.exists():
            return float("inf")
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            saved_at = data.get("_saved_at", 0)
            return (time.time() - saved_at) / 3600
        except Exception:
            return float("inf")

    async def _refresh_session(self, stealth_cfg: dict) -> None:
        """
        Visit the Walmart homepage via browser to renew the _abck cookie and
        other session state — without navigating to any product page.
        Called proactively before the session expires so the HTTP path keeps working.
        """
        log.info("Refreshing Walmart session cookies…")
        try:
            await self._ensure_browser(stealth_cfg)
        except Exception as e:
            log.warning(f"Could not start browser for session refresh: {e}")
            return

        page = None
        try:
            page = await self._context.new_page()
            await page.goto("https://www.walmart.com", wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            page_html = await page.content()
            if self._is_technical_error(page_html):
                log.warning("Walmart technical error page during session refresh — backing off 15 min.")
                self._browser_cooldown_until = time.time() + 900
                return
            if await self._has_press_hold(page):
                log.info("Challenge on homepage during session refresh — attempting to solve…")
                solved = await self._solve_press_and_hold(page)
                if not solved:
                    log.warning("Could not solve homepage challenge during session refresh")
                    return
                await page.wait_for_timeout(random.randint(2000, 3000))

            await page.evaluate(f"window.scrollBy({{top:{random.randint(100,300)},behavior:'smooth'}})")
            await page.wait_for_timeout(random.randint(1000, 2000))
            vp = self._viewport
            await page.mouse.move(
                random.randint(100, vp["width"] - 100),
                random.randint(100, vp["height"] - 100),
            )
            await page.wait_for_timeout(random.randint(500, 1000))
            await self._save_session()
            self._warmed_up = True
            log.info("Session refreshed successfully")
        except Exception as e:
            log.warning(f"Session refresh failed: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _check_via_browser_page(self, url: str, name: str):
        """
        Navigate to a product page using the persistent browser context
        (already warmed up via ISP proxy). Extract price from the rendered page.
        Returns (price, available, image_url).
        """
        page = None
        try:
            page = await self._context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            html = await page.content()

            if self._is_blocked(html):
                log.warning(f"{name}: bot wall on product page via browser")
                return None, False, None

            price = self._extract_price_from_json_ld(html)
            if price is None:
                price = self._extract_price_from_next_data(html)
            if price is None:
                price = await self._extract_price_from_dom(page)

            available = await self._check_availability(page, html)
            image_url = self._extract_image_url(html)

            return price, available, image_url
        except Exception as e:
            log.debug(f"{name}: browser page error: {e}")
            return None, False, None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _close_browser(self) -> None:
        """Shut down the browser to free resources after a warmup."""
        for obj in (self._context, self._browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._pw = None
        log.info("Browser closed after session warmup")

    async def _recreate_context(self, stealth_cfg: dict) -> None:
        log.warning("Recreating browser context…")
        self._warmed_up = False
        for obj in (self._context, self._browser, self._pw):
            try:
                if obj:
                    if hasattr(obj, 'stop'):
                        await obj.stop()
                    elif hasattr(obj, 'close'):
                        await obj.close()
            except Exception:
                pass
        self._pw = self._browser = self._context = None
        self._ua       = random.choice(USER_AGENTS)
        self._viewport = random.choice(VIEWPORTS)
        await self._ensure_browser(stealth_cfg)

    async def close(self) -> None:
        await self._save_session()
        for obj, method in ((self._context, 'close'), (self._browser, 'close'), (self._pw, 'stop')):
            try:
                if obj: await getattr(obj, method)()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Main check
    # ------------------------------------------------------------------

    async def check_product(self, product: dict, stealth_cfg: dict | None = None) -> ProductResult:
        stealth_cfg = stealth_cfg or {}
        url    = product["url"]
        name   = product["name"]
        result = ProductResult(name=name, url=url, price=None, available=False)

        # ── Path 0: Official Affiliate API (no bot risk) ──────────────
        api_cfg = stealth_cfg.get("walmart_api") or {}
        if api_cfg.get("enabled") and api_cfg.get("consumer_id"):
            api_result = await self._check_via_affiliate_api(url, name, api_cfg)
            if api_result is not None:
                return api_result
            log.debug("Affiliate API failed — falling through to HTTP check")

        # ── Path 1: ISP proxy session handoff ────────────────────────
        # Browser warms up on walmart.com (generates _abck cookie),
        # then curl-cffi uses that cookie + same ISP proxy for product pages.
        isp_proxy = self._next_isp_proxy(stealth_cfg)
        zip_code  = stealth_cfg.get("zip_code", "10001")

        if isp_proxy:
            pid = ProductResult.short_proxy_id(isp_proxy)

            # Warmup if session is stale or missing
            session_refresh_hours = stealth_cfg.get("session_refresh_hours", 3)
            if self._session_age_hours() >= session_refresh_hours:
                # Lock prevents parallel checks from all warming up simultaneously
                if not self._warmup_lock:
                    self._warmup_lock = asyncio.Lock()
                async with self._warmup_lock:
                    # Re-check after acquiring lock — another check may have warmed up already
                    if self._session_age_hours() >= session_refresh_hours:
                        log.info(f"Session stale — warming up via ISP proxy")
                        try:
                            await self._ensure_browser(stealth_cfg)
                            await self._warmup()
                        except Exception as e:
                            log.warning(f"ISP warmup failed: {e}")
                        finally:
                            try:
                                await self._close_browser()
                            except Exception:
                                pass

            # curl-cffi with ISP proxy + fresh cookies
            log.info(f"Trying HTTP check for: {name} (ISP #{pid})")
            html = await self._check_via_http(url, zip_code, proxy=isp_proxy)
            if html and not self._is_blocked(html):
                price = self._extract_price_from_json_ld(html)
                if price is None:
                    price = self._extract_price_from_next_data(html)
                if price is not None:
                    log.info(f"{name}: ${price:.2f} via ISP proxy")
                    result.price          = price
                    result.available      = self._check_availability_html(html)
                    result.image_url      = self._extract_image_url(html)
                    result.proxy_id       = pid
                    result.proxy_ok       = True
                    result.proxy_type     = "ISP"
                    result.response_bytes = self._last_response_bytes
                    return result

            result.error          = "ISP proxy check failed — will retry next cycle"
            result.proxy_id       = pid
            result.proxy_ok       = False
            result.proxy_type     = "ISP"
            result.response_bytes = self._last_response_bytes
            log.warning(f"{name}: ISP proxy path failed")
            return result

        # ── Path 2: residential proxy or direct (no ISP) ─────────────
        http_proxy = self._next_http_proxy(stealth_cfg)
        has_proxy  = http_proxy is not None
        pid        = ProductResult.short_proxy_id(http_proxy)

        log.info(f"Trying HTTP check for: {name}" + (f" (proxy #{pid})" if pid else ""))
        html = await self._check_via_http(url, zip_code, proxy=http_proxy)
        if html and not self._is_blocked(html):
            price = self._extract_price_from_json_ld(html)
            if price is None:
                price = self._extract_price_from_next_data(html)
            if price is not None:
                log.info(f"{name}: ${price:.2f} via HTTP")
                result.price          = price
                result.available      = self._check_availability_html(html)
                result.image_url      = self._extract_image_url(html)
                result.proxy_id       = pid
                result.proxy_ok       = True
                result.proxy_type     = "Resi"
                result.response_bytes = self._last_response_bytes
                return result

        # Direct fallback
        if has_proxy:
            html = await self._check_via_http(url, zip_code, proxy=None)
            if html and not self._is_blocked(html):
                price = self._extract_price_from_json_ld(html)
                if price is None:
                    price = self._extract_price_from_next_data(html)
                if price is not None:
                    log.info(f"{name}: ${price:.2f} via direct fallback")
                    result.price     = price
                    result.available = self._check_availability_html(html)
                    result.image_url = self._extract_image_url(html)
                    return result

        result.error = "All check paths failed — will retry next cycle"
        log.warning(f"{name}: all paths failed")
        return result

        if not html:
            log.debug("No saved session cookies — using browser")

        # ── Path 2: Full patchright browser (only when no proxies) ────
        if time.time() < self._browser_cooldown_until:
            secs = int(self._browser_cooldown_until - time.time())
            log.info(f"Browser on cooldown after repeated challenges — skipping for {secs}s")
            result.error = f"Cooling down ({secs}s remaining)"
            return result

        try:
            await self._ensure_browser(stealth_cfg)
            await self._warmup()
        except Exception as e:
            result.error = str(e)
            return result

        if not self._warmed_up:
            secs = int(self._browser_cooldown_until - time.time())
            if secs > 0:
                log.warning(f"Walmart flagged this session — browser on cooldown for {secs//60}m {secs%60}s")
                result.error = f"IP/session flagged — cooling down ({secs//60}m {secs%60}s)"
            else:
                result.error = "Warmup failed — will retry next cycle"
            return result

        page = None
        try:
            page = await self._context.new_page()
            await asyncio.sleep(random.uniform(1.0, 2.5))

            arrived = await self._navigate_via_search(page, name, url)
            if not arrived:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)

            await page.wait_for_timeout(random.randint(3000, 6000))
            await page.evaluate(f"window.scrollBy({{top:{random.randint(100,350)},behavior:'smooth'}})")
            await page.wait_for_timeout(random.randint(800, 1800))

            html = await page.content()

            # ── Press & Hold challenge ────────────────────────────────
            if await self._has_press_hold(page):
                solved = await self._solve_press_and_hold(page)
                if solved:
                    # Re-read the page after the challenge clears
                    await page.wait_for_timeout(random.randint(2000, 3500))
                    html = await page.content()
                    if await self._has_press_hold(page):
                        log.warning("Press & Hold challenge reappeared after hold — giving up")
                        result.blocked = True
                        result.error   = "Bot wall detected"
                        SESSION_FILE.unlink(missing_ok=True)
                        self._warmed_up = False
                        return result
                    log.info("Press & Hold solved — continuing with page data")
                else:
                    result.blocked = True
                    result.error   = "Bot wall detected"
                    SESSION_FILE.unlink(missing_ok=True)
                    self._warmed_up = False
                    return result

            if self._is_blocked(html):
                result.blocked = True
                result.error   = "Bot wall detected"
                SESSION_FILE.unlink(missing_ok=True)
                self._warmed_up = False
                return result

            price = self._extract_price_from_json_ld(html)
            if price is None:
                price = self._extract_price_from_next_data(html)
            if price is None:
                price = await self._extract_price_from_dom(page)

            result.price     = price
            result.available = await self._check_availability(page, html)
            result.image_url = self._extract_image_url(html)

            if price:
                result.raw_price_text = f"${price:.2f}"
                log.info(f"{name}: ${price:.2f} via browser | in stock: {result.available}")
            else:
                log.warning(f"{name}: could not parse price")

            await self._save_session()

        except Exception as e:
            result.error = str(e)
            log.error(f"Browser error for {name}: {e}")
            await self._recreate_context(stealth_cfg)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        return result

    # ------------------------------------------------------------------
    # Search-first navigation
    # ------------------------------------------------------------------

    async def _navigate_via_search(self, page, product_name: str, product_url: str) -> bool:
        try:
            keywords   = " ".join(product_name.split()[:4])
            search_url = f"https://www.walmart.com/search?q={quote_plus(keywords)}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            await page.evaluate(f"window.scrollBy({{top:{random.randint(200,600)},behavior:'smooth'}})")
            await page.wait_for_timeout(random.randint(1000, 2000))
            vp = self._viewport
            await page.mouse.move(
                random.randint(200, vp["width"] - 200),
                random.randint(200, min(600, vp["height"] - 100)),
            )
            await page.wait_for_timeout(random.randint(500, 1200))
            await page.goto(product_url, wait_until="domcontentloaded", timeout=35000)
            return True
        except Exception as e:
            log.warning(f"Search navigation failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Bot-wall / CAPTCHA detection and solving
    # ------------------------------------------------------------------

    def _is_blocked(self, html: str) -> bool:
        # If the page contains real product data it's not a block page —
        # PerimeterX embeds "captcha", "security check" etc. in its tracking
        # scripts on every Walmart page load, so keyword-only checks produce
        # false positives on successful product pages.
        if '__NEXT_DATA__' in html or 'itemprop="price"' in html:
            return False

        lower = html.lower()
        return any(s in lower for s in (
            "robot check", "are you a human", "robot or human",
            "unusual traffic", "automated access",
            "please verify you are a human", "verify your identity",
            "press & hold", "press and hold",
        ))

    def _is_technical_error(self, html: str) -> bool:
        """
        Walmart's Akamai soft-block page — displayed before PerimeterX loads.
        Looks like a real server error but is actually an IP/session flag.
        Distinct from the press-and-hold challenge; no interaction can solve it.
        The only remedy is to wait and let the flag expire.
        """
        lower = html.lower()
        return any(s in lower for s in (
            "we're having a technical error",
            "we're having a technical issue",
            "technical difficulties",
            "something went wrong on our end",
            "sorry, something went wrong",
        )) and '__NEXT_DATA__' not in html

    async def _has_press_hold(self, page) -> bool:
        """
        Returns True only when a visible Press & Hold challenge is actually on screen.
        Checking HTML source is unreliable — PerimeterX injects px-captcha scripts
        on every Walmart page load regardless of whether a challenge is active.
        """
        # Check main page for visible challenge container
        for sel in ('#px-captcha', '[id*="px-captcha"]', '[class*="px-captcha"]'):
            try:
                if await page.locator(sel).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue

        # Check all iframes — PerimeterX often renders the challenge in an iframe
        for frame in page.frames:
            try:
                btn = frame.locator('button').first
                if await btn.is_visible(timeout=500):
                    text = (await btn.inner_text(timeout=500)).lower()
                    if "hold" in text or "press" in text:
                        return True
            except Exception:
                continue

        return False

    async def _solve_press_and_hold(self, page) -> bool:
        """
        Solve the PerimeterX 'Press & Hold' CAPTCHA by simulating a sustained
        mouse-down event with realistic timing and natural cursor movement.

        With patchright + real non-headless Chrome these are genuine OS-level
        hardware events — PerimeterX cannot distinguish them from a human.
        Returns True if the challenge element was found and the hold was attempted.
        """
        log.info("Press & Hold challenge detected — attempting to solve…")

        # PerimeterX injects an iframe; we need to locate the button inside it
        btn = None

        # 1. Try direct page selectors first
        for sel in (
            '#px-captcha button',
            '[id*="px-captcha"] button',
            '[class*="captcha"] button',
            'button[class*="hold"]',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    btn = loc
                    log.debug(f"Found hold button via selector: {sel}")
                    break
            except Exception:
                continue

        # 2. Search inside all iframes
        if not btn:
            for frame in page.frames:
                for sel in (
                    'button',
                    '[id*="px-captcha"]',
                    '[class*="captcha"]',
                ):
                    try:
                        loc = frame.locator(sel).first
                        if await loc.is_visible(timeout=1000):
                            btn = loc
                            log.debug(f"Found hold button in iframe via: {sel}")
                            break
                    except Exception:
                        continue
                if btn:
                    break

        if not btn:
            log.warning("Could not locate Press & Hold button — cannot auto-solve")
            return False

        try:
            box = await btn.bounding_box()
            if not box:
                log.warning("Hold button has no bounding box")
                return False

            cx = box["x"] + box["width"]  / 2
            cy = box["y"] + box["height"] / 2

            # Natural cursor approach: two-step movement with slight overshoot
            await page.mouse.move(
                cx + random.randint(-40, 40),
                cy + random.randint(-20, 20),
            )
            await page.wait_for_timeout(random.randint(200, 500))
            await page.mouse.move(cx, cy)
            await page.wait_for_timeout(random.randint(150, 350))

            # Hold for 10–14 s — randomised to avoid fixed-duration detection
            hold_ms = random.randint(10_000, 14_000)
            log.info(f"Holding mouse button for {hold_ms / 1000:.1f}s…")
            await page.mouse.down()
            await page.wait_for_timeout(hold_ms)
            await page.mouse.up()

            # Give PerimeterX time to validate and redirect
            await page.wait_for_timeout(random.randint(3000, 5000))
            log.info("Hold complete — waiting for challenge response")
            return True

        except Exception as e:
            log.warning(f"Press & Hold solve error: {e}")
            return False

    # ------------------------------------------------------------------
    # Price / availability extraction
    # ------------------------------------------------------------------

    def _extract_price_from_json_ld(self, html: str) -> Optional[float]:
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        ):
            try:
                data  = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") in ("Product", "Offer"):
                        offers  = item.get("offers", item)
                        targets = offers if isinstance(offers, list) else [offers]
                        for o in targets:
                            p = self._parse_price(str(o.get("price", "")))
                            if p:
                                return p
            except Exception:
                continue
        return None

    def _extract_price_from_next_data(self, html: str) -> Optional[float]:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return None
        try:
            text = m.group(1)
            for key in ('"currentPrice"', '"priceInfo"', '"price"'):
                idx = text.find(key)
                while idx != -1:
                    snippet = text[idx:idx+80]
                    nm = re.search(r'[\d]+\.[\d]{2}', snippet)
                    if nm:
                        val = float(nm.group())
                        if 1.0 < val < 10000.0:
                            return val
                    idx = text.find(key, idx + 1)
        except Exception:
            pass
        return None

    async def _extract_price_from_dom(self, page) -> Optional[float]:
        for sel in ('[itemprop="price"]', '[data-testid="price-wrap"] span',
                    'span.price-characteristic', '.price-group', 'span[class*="price"]'):
            try:
                p = self._parse_price(await page.locator(sel).first.inner_text(timeout=2000))
                if p:
                    return p
            except Exception:
                continue
        try:
            body  = await page.inner_text("body")
            cands = [float(ps.replace(",","")) for ps in re.findall(r'\$\s*([\d,]+\.[\d]{2})', body)
                     if 5.0 < float(ps.replace(",","")) < 5000.0]
            if cands:
                return min(cands)
        except Exception:
            pass
        return None

    def _extract_image_url(self, html: str) -> Optional[str]:
        """Extract the main product image URL from the page HTML."""
        # 1. JSON-LD Product schema — most reliable
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        ):
            try:
                data  = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Product":
                        img = item.get("image")
                        if isinstance(img, list):
                            img = img[0]
                        if isinstance(img, dict):
                            img = img.get("url")
                        if img and img.startswith("http"):
                            return img
            except Exception:
                continue

        # 2. og:image meta tag
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.IGNORECASE)
        if m:
            url = m.group(1)
            if url.startswith("http"):
                return url

        # 3. __NEXT_DATA__ — look for "imageInfo" or "images" keys
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd:
            text = nd.group(1)
            for key in ('"thumbnailUrl"', '"imageUrl"', '"url"'):
                idx = text.find(key)
                while idx != -1:
                    snippet = text[idx:idx+200]
                    um = re.search(r'https://[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*', snippet, re.IGNORECASE)
                    if um:
                        return um.group(0)
                    idx = text.find(key, idx + 1)

        return None

    def _check_availability_html(self, html: str) -> bool:
        """
        Determine availability from structured data first (JSON-LD, __NEXT_DATA__),
        falling back to a narrow text scan only when structured data is absent.
        """
        # 1. JSON-LD — most reliable (schema.org availability field)
        avail = self._availability_from_json_ld(html)
        if avail is not None:
            return avail

        # 2. __NEXT_DATA__ — Walmart embeds availabilityStatus / offerType
        avail = self._availability_from_next_data(html)
        if avail is not None:
            return avail

        # 3. Narrow text scan — only check the main product section (first 40 KB)
        #    to avoid false positives from ads, related products, and scripts.
        chunk = html[:40_000].lower()

        # Out-of-stock signals (check first — these override everything)
        for pat in ("out of stock", "currently unavailable", "sold out",
                    "get in-stock alert", "notify me when", "email me when",
                    "not available", "item not available"):
            if pat in chunk:
                return False

        # In-stock signals — require a positive match, don't assume
        if "add to cart" in chunk:
            return True

        # No signal either way — default to NOT available (conservative)
        return False

    def _availability_from_json_ld(self, html: str) -> Optional[bool]:
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            try:
                data  = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Product", "Offer"):
                        continue
                    offers  = item.get("offers", item)
                    targets = offers if isinstance(offers, list) else [offers]
                    for o in targets:
                        av = str(o.get("availability", "")).lower()
                        if "instock" in av or "limitedavailability" in av:
                            return True
                        if any(s in av for s in (
                            "outofstock", "soldout", "discontinued",
                            "preorder", "backorder",
                        )):
                            return False
            except Exception:
                continue
        return None

    def _availability_from_next_data(self, html: str) -> Optional[bool]:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return None
        text = m.group(1)

        # Count all availabilityStatus values — a product page has multiple
        # (main offer, marketplace sellers, pickup, delivery, etc.).
        # If ANY says OUT_OF_STOCK or NOT_AVAILABLE and NONE says IN_STOCK
        # for the *primary* offer, it's out of stock.
        statuses = re.findall(r'"availabilityStatus"\s*:\s*"([^"]+)"', text)
        if not statuses:
            if '"availableOnline":true' in text:
                return True
            if '"availableOnline":false' in text:
                return False
            return None

        # The primary/first offer on Walmart is usually "ONLINE_ONLY" type.
        # If we find both IN_STOCK and OUT_OF_STOCK, look at the offerType
        # context.  As a heuristic: if the majority are out of stock, report
        # out of stock.
        in_count  = sum(1 for s in statuses if s == "IN_STOCK")
        out_count = sum(1 for s in statuses if s in ("OUT_OF_STOCK", "NOT_AVAILABLE"))

        if in_count > 0 and out_count == 0:
            return True
        if out_count > 0 and in_count == 0:
            return False

        # Mixed signals — look for "Add to Cart" as the tiebreaker
        if "add to cart" in text.lower() or "addToCart" in text:
            return True
        return False

    async def _check_availability(self, page, html: str) -> bool:
        # Structured data first
        avail = self._availability_from_json_ld(html)
        if avail is not None:
            return avail
        avail = self._availability_from_next_data(html)
        if avail is not None:
            return avail
        # DOM check — look at the actual Add to Cart button
        try:
            btn  = page.locator(
                '[data-testid="add-to-cart-section"] button,'
                'button[data-testid="add-to-cart-button"]'
            ).first
            text = (await btn.inner_text(timeout=2000)).lower()
            if "add to cart" in text or "add to registry" in text:
                return await btn.get_attribute("disabled") is None
        except Exception:
            pass
        # Fallback text scan of rendered page
        return self._check_availability_html(html)

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(r'[\d,]+\.[\d]{2}', text.replace(",", ""))
        if m:
            try:
                val = float(m.group().replace(",", ""))
                if 0.5 < val < 50000:
                    return val
            except ValueError:
                pass
        return None
