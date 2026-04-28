"""
Mana Pool marketplace monitor.

Uses the Mana Pool REST API (v1) to fetch prices and availability.
Requires an API token stored as `manapool_api_token` in config.json.

The API exposes three bulk endpoints that return every in-stock product at once.
We cache these responses and serve per-card lookups from the cache, refreshing
every CACHE_TTL_SECONDS so a single token covers all monitored products.

URL format understood by this monitor:
  https://manapool.com/card/{set}/{number}/{slug}?conditions=NM,LP&finish=foil
  https://manapool.com/sealed/{set}/{slug}

Fields returned (prices in dollars):
  price      — market price for the requested finish
  low_price  — lowest active listing matching requested conditions + finish
  available  — True if any copies available
  tcg_quantity — total copies available matching filters
"""

import asyncio
import json
import logging
import time
import urllib.request
from typing import Optional
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("manapool")

CACHE_TTL_SECONDS = 1800  # refresh every 30 minutes

_FINISH_MAP = {"foil": "FO", "nonfoil": "NF", "non-foil": "NF", "etched": "ET"}
_CONDITION_IDS = {"NM", "LP", "MP", "HP", "DMG"}

# condition groups: "lp_plus" covers LP, MP, HP, DMG — matches manapool's price tier
_LP_PLUS = {"LP", "MP", "HP", "DMG"}


class ManaPoolMonitor:

    def __init__(self):
        self._singles_cache: dict = {}   # (set_code, number) → row
        self._variants_cache: dict = {}  # (set_code, number, cond, finish) → row
        self._sealed_cache: dict = {}    # url → row
        self._cache_ts: float = 0.0
        self._api_token: Optional[str] = None

    def set_token(self, token: str) -> None:
        self._api_token = token

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def check_product(self, product: dict, stealth_cfg=None):
        from monitors.walmart_monitor import ProductResult
        url  = product["url"]
        name = product["name"]
        tags = product.get("tags", {})

        if not self._api_token:
            return ProductResult(name=name, url=url, price=None,
                                 available=False, error="No ManaPool API token configured")

        await self._refresh_cache_if_stale()

        try:
            market_price, low_price, quantity, image_url = self._lookup(url, tags)
        except Exception as e:
            log.warning(f"ManaPool {name}: lookup error {e}")
            return ProductResult(name=name, url=url, price=None,
                                 available=False, error=str(e))

        log.info(f"ManaPool {name}: market=${market_price}, low=${low_price}, qty={quantity}")
        return ProductResult(
            name=name, url=url,
            price=market_price,
            low_price=low_price,
            available=(quantity or 0) > 0,
            tcg_quantity=quantity,
            image_url=image_url,
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    async def _refresh_cache_if_stale(self) -> None:
        if time.time() - self._cache_ts < CACHE_TTL_SECONDS:
            return
        try:
            singles, variants, sealed = await asyncio.gather(
                asyncio.get_event_loop().run_in_executor(None, self._fetch_singles),
                asyncio.get_event_loop().run_in_executor(None, self._fetch_variants),
                asyncio.get_event_loop().run_in_executor(None, self._fetch_sealed),
            )
            self._singles_cache  = singles
            self._variants_cache = variants
            self._sealed_cache   = sealed
            self._cache_ts = time.time()
            log.info(f"ManaPool cache refreshed: {len(singles)} singles, "
                     f"{len(variants)} variants, {len(sealed)} sealed")
        except Exception as e:
            log.warning(f"ManaPool cache refresh failed: {e}")

    def _fetch_singles(self) -> dict:
        data = self._api_get("/api/v1/prices/singles")
        out = {}
        for row in data.get("data", []):
            key = (row.get("set_code", "").upper(), str(row.get("number", "")))
            out[key] = row
        return out

    def _fetch_variants(self) -> dict:
        data = self._api_get("/api/v1/prices/variants")
        out = {}
        for row in data.get("data", []):
            key = (
                row.get("set_code", "").upper(),
                str(row.get("number", "")),
                row.get("condition_id", ""),
                row.get("finish_id", ""),
            )
            out[key] = row
        return out

    def _fetch_sealed(self) -> dict:
        data = self._api_get("/api/v1/prices/sealed")
        out = {}
        for row in data.get("data", []):
            url = row.get("url", "")
            if url:
                out[url] = row
        return out

    def _api_get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"https://manapool.com{path}",
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Accept": "application/json",
                "User-Agent": "mtg-monitor/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    # ------------------------------------------------------------------
    # Per-product lookup from cache
    # ------------------------------------------------------------------

    def _lookup(self, url: str, tags: dict) -> tuple:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        path_parts = [p for p in parsed.path.split("/") if p]

        # Detect sealed vs. single
        is_sealed = path_parts[0] == "sealed" if path_parts else False

        if is_sealed:
            return self._lookup_sealed(url)

        # Card URL: /card/{set}/{number}/{slug}
        if len(path_parts) >= 3 and path_parts[0] == "card":
            set_code = path_parts[1].upper()
            number   = path_parts[2]
        else:
            raise ValueError(f"Cannot parse ManaPool card URL: {url}")

        # Condition/finish from URL params, falling back to tags
        conditions_raw = qs.get("conditions", [tags.get("condition", "NM")])[0]
        conditions = {c.strip().upper() for c in conditions_raw.split(",")} & _CONDITION_IDS
        if not conditions:
            conditions = {"NM"}

        finish_raw = qs.get("finish", [tags.get("printing", "nonfoil")])[0].lower()
        if finish_raw == "foil" or tags.get("printing", "").lower() == "foil":
            finish_id = "FO"
        else:
            finish_id = "NF"

        # Market price from singles cache
        single = self._singles_cache.get((set_code, number))
        if finish_id == "FO":
            market_raw = (single or {}).get("price_market_foil") or (single or {}).get("price_market")
        else:
            market_raw = (single or {}).get("price_market") or (single or {}).get("price_market_foil")
        market_price = round(market_raw / 100, 2) if market_raw else None
        image_url = (single or {}).get("image_url")

        # Low price + quantity from variants cache (matching conditions + finish)
        all_prices = []
        total_qty  = 0
        for cond in conditions:
            row = self._variants_cache.get((set_code, number, cond, finish_id))
            if row:
                lp = row.get("low_price")
                qty = row.get("available_quantity", 0)
                if lp:
                    all_prices.append(lp / 100)
                total_qty += qty  # count qty regardless of whether a low price exists

        # If no qty found from condition-specific variants, fall back to the
        # total available_quantity on the singles row (covers all conditions)
        if total_qty == 0 and single:
            fallback_qty = single.get("available_quantity", 0) or 0
            total_qty = fallback_qty

        low_price = round(min(all_prices), 2) if all_prices else None
        return market_price, low_price, total_qty, image_url

    def _lookup_sealed(self, url: str) -> tuple:
        # Normalize URL to strip query params for sealed lookup
        clean_url = url.split("?")[0].rstrip("/")
        row = self._sealed_cache.get(clean_url)
        if not row:
            raise ValueError(f"Sealed product not found in ManaPool cache: {clean_url}")
        market_raw = row.get("price_market")
        low_raw    = row.get("low_price")
        market_price = round(market_raw / 100, 2) if market_raw else None
        low_price    = round(low_raw / 100, 2) if low_raw else None
        qty = row.get("available_quantity", 0)
        return market_price, low_price, qty, None
