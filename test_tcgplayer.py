"""
TCGPlayer diagnostic script.
Run from the mtg-monitor directory:
    python test_tcgplayer.py

Shows exactly what we're getting from TCGPlayer so we can fix the parser.
"""

import asyncio
import json
import re
import sys

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PRODUCT_URL = "https://www.tcgplayer.com/product/675558/Magic-Secrets%20of%20Strixhaven-Secrets%20of%20Strixhaven%20Collector%20Booster%20Display?Language=English"
PRODUCT_ID  = "675558"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

PAGE_HEADERS = {
    "User-Agent":         UA,
    "Accept":             "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "Sec-Fetch-Dest":     "document",
    "Sec-Fetch-Mode":     "navigate",
    "Sec-Fetch-Site":     "none",
}

API_HEADERS = {
    "User-Agent":      UA,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.tcgplayer.com",
    "Referer":         "https://www.tcgplayer.com/",
}


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


async def fetch_page(url, headers):
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome124") as s:
            r = await s.get(url, headers=headers, timeout=20, allow_redirects=True)
            # Decode bytes explicitly to avoid cp1252 issues on Windows
            text = r.content.decode("utf-8", errors="replace")
            print(f"  curl-cffi -> HTTP {r.status_code}  ({len(text)} chars)")
            return r.status_code, text
    except ImportError:
        pass
    except Exception as e:
        print(f"  curl-cffi error: {e}")

    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            raw  = await r.read()
            text = raw.decode("utf-8", errors="replace")
            print(f"  aiohttp -> HTTP {r.status}  ({len(text)} chars)")
            return r.status, text


def check_next_data(html):
    section("__NEXT_DATA__ check")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        print("  NOT FOUND — page likely does not embed __NEXT_DATA__")
        return

    raw = m.group(1)
    print(f"  Found — {len(raw)} chars")

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"  JSON parse error: {e}")
        print(f"  First 300 chars: {raw[:300]}")
        return

    # Show top-level structure
    print(f"  Top-level keys: {list(data.keys())}")
    if "props" in data:
        props = data["props"]
        print(f"  props keys: {list(props.keys())}")
        if "pageProps" in props:
            pp = props["pageProps"]
            print(f"  pageProps keys: {list(pp.keys())[:20]}")

    # Deep-search for price-related keys
    price_keys = ["marketPrice", "market_price", "lowestListingPrice", "lowPrice",
                  "low_price", "directLowPrice", "totalListings", "listedCount"]
    found = {}
    _deep_search(data, price_keys, found, depth=0)

    if found:
        print(f"\n  Price-related keys found in JSON tree:")
        for k, v in found.items():
            print(f"    {k}: {v}")
    else:
        print("\n  No price-related keys found anywhere in the JSON tree")
        print("  Prices are likely loaded client-side via a separate API call")


def _deep_search(obj, keys, found, depth):
    if depth > 15:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                found[k] = v
            else:
                _deep_search(v, keys, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _deep_search(item, keys, found, depth + 1)


def check_script_tags(html):
    section("Script tags with pricing keywords")
    keywords = ["marketPrice", "lowestListingPrice", "lowPrice", "market_price"]
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    print(f"  Total <script> blocks: {len(scripts)}")
    hits = 0
    for i, s in enumerate(scripts):
        for kw in keywords:
            if kw in s:
                print(f"  Script #{i} contains '{kw}' ({len(s)} chars total)")
                # Show 200 chars around the keyword
                idx = s.find(kw)
                snippet = s[max(0, idx-40): idx+80]
                print(f"    ...{snippet}...")
                hits += 1
                break
    if hits == 0:
        print("  No pricing keywords found in any script block")


async def check_api_endpoints():
    section("API endpoint probes")

    get_endpoints = [
        (f"https://mp-search-api.tcgplayer.com/v1/product/{PRODUCT_ID}/listings?mpfev=3&channel=0&language=1&start=0&rows=5&sort[]=price+asc",
         "mp-search-api listings (GET)"),
        (f"https://api.tcgplayer.com/v1.39.0/pricing/product/{PRODUCT_ID}",
         "api.tcgplayer pricing v1.39 (needs auth)"),
        (f"https://api.tcgplayer.com/catalog/products/{PRODUCT_ID}",
         "api.tcgplayer catalog (needs auth)"),
    ]

    for url, label in get_endpoints:
        print(f"\n  [{label}]")
        print(f"  {url}")
        try:
            status, text = await fetch_page(url, API_HEADERS)
            if text and len(text) > 2:
                try:
                    data = json.loads(text)
                    print(f"  -> valid JSON ({status}), top-level keys: {list(data.keys())[:10]}")
                    found = {}
                    _deep_search(data, ["marketPrice", "lowestListingPrice", "lowPrice",
                                        "market_price", "totalListings", "price"], found, 0)
                    if found:
                        print(f"  -> PRICE DATA: {found}")
                    else:
                        print(f"  -> No price keys. First 400 chars: {text[:400]}")
                except Exception:
                    print(f"  -> Not JSON ({status}). First 200 chars: {text[:200]}")
        except Exception as e:
            print(f"  -> Error: {e}")

    # mp-search-api POST — try multiple body formats to get listing prices
    print(f"\n  [mp-search-api listings POST — body variants]")
    post_url = f"https://mp-search-api.tcgplayer.com/v1/product/{PRODUCT_ID}/listings"
    bodies = [
        {"mpfev": 3, "channel": 0, "language": 1, "start": 0, "rows": 10},
        {"start": 0, "rows": 10},
        {"listingSearch": {"start": 0, "rows": 10}},
        {"listingSearch": {"start": 0, "rows": 10, "filters": {}}},
        {"mpfev": 3, "listingSearch": {"start": 0, "rows": 10}},
    ]
    from curl_cffi.requests import AsyncSession
    for body in bodies:
        print(f"\n  body={body}")
        async with AsyncSession(impersonate="chrome124") as s:
            r = await s.post(post_url, json=body, headers=API_HEADERS, timeout=15)
            text = r.content.decode("utf-8", errors="replace")
            print(f"  -> HTTP {r.status_code}")
            try:
                data = json.loads(text)
                inner = data.get("results", [{}])[0] if data.get("results") else {}
                results_list = inner.get("results", [])
                print(f"  -> totalResults={inner.get('totalResults')}  inner results count={len(results_list)}")
                if results_list:
                    print(f"  -> FIRST LISTING: {json.dumps(results_list[0], indent=2)[:800]}")
                    break
            except Exception:
                print(f"  -> {text[:200]}")


def check_og_meta(html):
    section("og:title / og:description (sanity check)")
    for prop in ["og:title", "og:description", "og:image"]:
        m = re.search(rf'property=["\']og:{prop.split(":")[1]}["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(rf'content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop.split(":")[1]}["\']', html, re.IGNORECASE)
        val = m.group(1)[:100] if m else "NOT FOUND"
        print(f"  {prop}: {val}")


async def main():
    print(f"\nTCGPlayer Diagnostic")
    print(f"URL: {PRODUCT_URL}")

    section("Fetching product page")
    status, html = await fetch_page(PRODUCT_URL, PAGE_HEADERS)

    if not html or status != 200:
        print(f"  FAILED — got HTTP {status}. Cannot continue.")
        sys.exit(1)

    section("First 1500 chars of HTML")
    print(html[:1500])

    check_og_meta(html)
    check_next_data(html)
    check_script_tags(html)
    await check_api_endpoints()

    section("Done")
    print("  Share the output above to diagnose the pricing issue.\n")


asyncio.run(main())
